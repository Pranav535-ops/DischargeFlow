"""The assistant: LLM tool calling over the service layer (ADR-001 Phase 3).

Safety properties, in order of importance:
1. The model can only read through tools. Writes become AssistantAction proposals that run when the same user
   confirms them (POST /chatbot/actions/{id}/confirm); the model never executes a write.
2. Every tool call is permission-checked like its HTTP endpoint and audited.
3. The model sees de-identified text only: patients are tokens, and tool results are wrapped as <tool_data> that the
   system prompt marks as untrusted data, never instructions (prompt-injection boundary).
"""
import hashlib
import json
import logging
import re
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.ai.llm import LLMClient, LLMError, MockLLMClient, get_llm_client
from app.ai.models import AIGeneration
from app.ai.prompts import get_prompt
from app.audit.service import write_audit
from app.chatbot.models import AssistantAction
from app.chatbot.tools import (TOOLS, ToolContext, ToolInputError, as_tool_data, describe_write, http_error_message,
                               prepare_write, tool_definitions)
from app.core.db import DB, utcnow
from app.core.security import AuthUser, CurrentUser, ensure_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chatbot", tags=["Chatbot"])

MAX_ROUNDS = 5
MAX_HISTORY = 10
ASSISTANT_MAX_TOKENS = 4000
ACTION_TTL = timedelta(minutes=10)
# Arguments whose values are record ids, kept in the append-only audit log. Every other argument is recorded by name
# only: titles, descriptions and free-text references can carry PHI, and audit rows outlive erasure.
AUDIT_ID_KEYS = ("patient_id", "workflow_id", "task_id", "claim_id")
_ID_RE = re.compile(r"^[a-z]+_[0-9a-f]{6,32}$")


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    # Prior turns shown in the chat window, so follow-ups ("and the second one?") have context.
    history: list[ChatTurn] = Field(default_factory=list)


def assistant_client() -> LLMClient:
    """The configured model, or the offline rule-based mock when no provider is set up.

    Unlike summaries, the mock is a safe fallback here: it writes no clinical text, it only routes requests to the
    same permission-checked tools.
    """
    try:
        return get_llm_client("assistant")
    except HTTPException:
        return MockLLMClient()


def _history(ctx: ToolContext, turns: list[ChatTurn]) -> list[dict]:
    turns = turns[-MAX_HISTORY:]
    while turns and turns[0].role != "user":
        turns = turns[1:]
    return [{"role": t.role, "content": ctx.deid.redact(t.content)} for t in turns if t.content.strip()]


def _record_call(db, user: CurrentUser, client: LLMClient, prompt_id: str, prompt_sha: str, messages: list[dict],
                 resp=None, error: Optional[str] = None) -> None:
    digest = hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    db.add(AIGeneration(
        purpose="assistant", status="succeeded" if resp else "failed", user_id=user.user_id,
        provider=client.provider, model_id=resp.model_id if resp else client.model_id, prompt_version=prompt_id,
        prompt_sha256=prompt_sha, input_hash=digest, input_tokens=resp.input_tokens if resp else 0,
        output_tokens=resp.output_tokens if resp else 0, latency_ms=resp.latency_ms if resp else 0, error=error,
    ))


def _tool_audit_details(ctx: ToolContext, tool_name: str, raw_input, ids: Optional[dict] = None) -> str:
    """Audit details for one tool call: its name, argument names and record ids, never argument values."""
    raw = raw_input if isinstance(raw_input, dict) else {}
    details: dict = {"tool": tool_name, "arg_keys": sorted(str(k) for k in raw)}
    ref = raw.get("patient_ref")
    if isinstance(ref, str) and ref.strip() in ctx.patient_ids:
        details["patient_id"] = ctx.patient_ids[ref.strip()]
    for key in AUDIT_ID_KEYS:
        value = (ids or {}).get(key, raw.get(key))
        if isinstance(value, str) and _ID_RE.match(value):
            details[key] = value
    return json.dumps(details, sort_keys=True)


async def _run_tool(ctx: ToolContext, call, ui_data: dict, pending: list[dict]) -> dict:
    """Execute a read or create a write proposal. Returns the tool_result block for the model."""
    tool = TOOLS.get(call.name)
    if tool is None:
        return {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": "Unknown tool."}
    action = f"ASSISTANT_{'PROPOSE' if tool.writes else 'TOOL'}_{tool.name.upper()}"
    try:
        await ensure_permission(ctx.db, ctx.user, tool.permission)
    except HTTPException:
        await write_audit(ctx.db, ctx.user, f"ASSISTANT_DENIED_{tool.name.upper()}", "chatbot", None,
                          _tool_audit_details(ctx, tool.name, call.input), ctx.request)
        return {"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                "content": "Permission denied: your role is not allowed to do this."}
    try:
        if tool.writes:
            arguments = prepare_write(ctx, tool, call.input)
            proposal = AssistantAction(user_id=ctx.user.user_id, tool=tool.name, arguments=arguments,
                                       expires_at=utcnow() + ACTION_TTL)
            ctx.db.add(proposal)
            await ctx.db.flush()
            summary = await describe_write(ctx, tool.name, arguments)
            pending.append({"action_id": proposal.action_id, "tool": tool.name, "summary": summary,
                            "expires_at": proposal.expires_at.isoformat()})
            await write_audit(ctx.db, ctx.user, action, "chatbot", proposal.action_id,
                              _tool_audit_details(ctx, tool.name, call.input, arguments), ctx.request)
            content = as_tool_data(tool.name, {"proposal": "created", "action_id": proposal.action_id,
                                               "status": "awaiting user confirmation; nothing has changed yet"})
        else:
            args = tool.args.model_validate(call.input)
            out = await tool.run(ctx, args)
            for key, value in (out.ui_data or {}).items():
                ui_data[key] = value
            await write_audit(ctx.db, ctx.user, action, "chatbot", out.resource_id,
                              _tool_audit_details(ctx, tool.name, call.input), ctx.request)
            content = as_tool_data(tool.name, out.model_data)
    except (ValidationError, ToolInputError) as e:
        return {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": f"Invalid input: {e}"[:1000]}
    except HTTPException as e:
        return {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": http_error_message(e)}
    return {"type": "tool_result", "tool_use_id": call.id, "content": content}


@router.post("/message")
async def chatbot_message(req: ChatRequest, request: Request, db: DB, user: AuthUser):
    ctx = await ToolContext.build(db, user, request)
    client = assistant_client()
    prompt = get_prompt("assistant")
    system = prompt.render_system(role=user.role)
    tools = tool_definitions()
    user_text = ctx.deid.redact(req.message)
    messages = _history(ctx, req.history) + [{"role": "user", "content": user_text}]

    ui_data: dict = {}
    pending: list[dict] = []
    called: list[str] = []
    final_text = ""
    for _ in range(MAX_ROUNDS):
        try:
            resp = await client.complete(system=system, messages=messages, max_tokens=ASSISTANT_MAX_TOKENS,
                                         tools=tools)
        except LLMError as e:
            _record_call(db, user, client, prompt.id, prompt.sha256, messages, error=str(e)[:500])
            await write_audit(db, user, "CHATBOT_MESSAGE", "chatbot", None,
                              json.dumps({"status": "failed", "error": type(e).__name__,
                                          "message_length": len(req.message), "tools": called}), request)
            return JSONResponse(status_code=502, content={"detail": "The assistant is unavailable right now."})
        _record_call(db, user, client, prompt.id, prompt.sha256, messages, resp)
        if not resp.tool_calls:
            final_text = resp.text
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for call in resp.tool_calls:
            called.append(call.name)
            results.append(await _run_tool(ctx, call, ui_data, pending))
        messages.append({"role": "user", "content": results})
    else:
        final_text = "I couldn't complete that request in a few steps. Please try asking more specifically."

    await write_audit(db, user, "CHATBOT_MESSAGE", "chatbot", None,
                      json.dumps({"message_length": len(req.message), "tools": called}), request)
    return {
        "intent": called[0] if called else "chat",
        "message": req.message,
        "response": ctx.deid.reidentify(final_text),
        "data": ui_data or None,
        "pending_actions": pending,
        "tool_calls": called,
        "provider": client.provider,
    }


async def _pending_action(db, user: CurrentUser, action_id: str) -> AssistantAction:
    action = await db.scalar(
        select(AssistantAction).where(AssistantAction.action_id == action_id).with_for_update())
    if action is None or action.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"Action already {action.status}")
    return action


@router.post("/actions/{action_id}/confirm")
async def confirm_action(action_id: str, request: Request, db: DB, user: AuthUser):
    """Run a proposed write. Only its proposer can confirm it, once, before it expires."""
    action = await _pending_action(db, user, action_id)
    tool = TOOLS[action.tool]
    if action.expires_at <= utcnow():
        action.status, action.resolved_at = "expired", utcnow()
        return JSONResponse(status_code=410, content={"detail": "This proposal expired; ask the assistant again."})
    # Permissions are checked again: the role may have changed since the proposal.
    await ensure_permission(db, user, tool.permission)
    ctx = await ToolContext.build(db, user, request)
    out = await tool.run(ctx, dict(action.arguments))
    action.status, action.resolved_at = "confirmed", utcnow()
    action.result = {"resource_id": out.resource_id}
    await write_audit(db, user, f"ASSISTANT_CONFIRMED_{tool.name.upper()}", "chatbot", out.resource_id,
                      f"action {action_id}", request)
    return {"status": "confirmed", "response": out.model_data.get("message", "Done."), "data": out.ui_data}


@router.post("/actions/{action_id}/cancel")
async def cancel_action(action_id: str, request: Request, db: DB, user: AuthUser):
    action = await _pending_action(db, user, action_id)
    action.status, action.resolved_at = "cancelled", utcnow()
    await write_audit(db, user, f"ASSISTANT_CANCELLED_{action.tool.upper()}", "chatbot", None,
                      f"action {action_id}", request)
    return {"status": "cancelled"}

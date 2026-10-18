"""LLM gateway: one provider-agnostic interface for every model call in the app.

Messages and tool definitions use the Messages API shape (role + content blocks), which both real adapters speak
natively; the mock adapter interprets the same shape offline. Callers never import a provider SDK directly.

Providers (LLM_PROVIDER):
- "mock": deterministic, offline (tests, CI evals, local development). It only restates facts it is given.
- "anthropic": Claude through the Anthropic API (ANTHROPIC_API_KEY).
- "bedrock": Claude on Amazon Bedrock (AWS credentials + AWS_REGION), the HIPAA-eligible path in ADR-001.
- "none": AI generation disabled (the production default until a provider is chosen).
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

Purpose = Literal["summary", "assistant"]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    model_id: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    # The assistant turn exactly as returned, to append when continuing a tool-use loop.
    content: list[dict] = field(default_factory=list)


class LLMError(Exception):
    """The provider failed or declined; the caller decides how to surface it."""


class LLMClient(Protocol):
    provider: str
    model_id: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
    ) -> LLMResponse: ...


# ---------- Claude (Anthropic API / Bedrock) ----------

class ClaudeClient:
    """Claude through the Anthropic SDK; the same request shape works on the Anthropic API and on Bedrock."""

    def __init__(self, sdk_client: Any, provider: str, model_id: str, effort: str):
        self._sdk = sdk_client
        self.provider = provider
        self.model_id = model_id
        self._effort = effort

    async def complete(self, *, system, messages, max_tokens, tools=None, json_schema=None) -> LLMResponse:
        import anthropic

        output_config: dict = {"effort": self._effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        kwargs: dict = {"model": self.model_id, "max_tokens": max_tokens, "system": system, "messages": messages,
                        "output_config": output_config}
        if tools:
            kwargs["tools"] = tools
        started = time.perf_counter()
        try:
            msg = await self._sdk.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            logger.error("LLM %s error %s (request %s)", self.provider, e.status_code, e.request_id)
            raise LLMError(f"provider returned {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("provider unreachable") from e
        latency_ms = int((time.perf_counter() - started) * 1000)
        if msg.stop_reason == "refusal":
            # No silent fallback to another model: provenance must name the model that wrote the text.
            raise LLMError("model declined the request")

        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in msg.content if b.type == "tool_use"]
        return LLMResponse(
            text=text, tool_calls=calls, stop_reason=msg.stop_reason or "", model_id=msg.model or self.model_id,
            provider=self.provider, input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
            latency_ms=latency_ms, content=[b.model_dump(exclude_none=True) for b in msg.content],
        )


# ---------- Mock (offline, deterministic) ----------

FACTS_RE = re.compile(r"<patient_facts>\s*(\{.*?\})\s*</patient_facts>", re.S)
NOTE_FIELDS = {"course": "course", "procedures": "procedures", "medications": "medications",
               "follow-up": "follow_up", "follow up": "follow_up", "warning signs": "warning_signs",
               "activity": "activity", "diet": "diet"}


def _note_sections(notes: str) -> dict[str, str]:
    """'Label: value' lines from clinical notes, which is all the mock can 'understand'."""
    out: dict[str, str] = {}
    for line in notes.splitlines():
        label, sep, value = line.partition(":")
        key = NOTE_FIELDS.get(label.strip().lower())
        if sep and key and value.strip():
            out[key] = value.strip()
    return out


def _split(value: str) -> list[str]:
    return [v.strip() for v in re.split(r";|\n", value) if v.strip()]


def _mock_medication(entry: str) -> dict:
    # "amoxicillin 500 mg three times daily for 5 days" -> name + the rest as instructions.
    name, _, rest = entry.partition(" ")
    return {"name": name, "dose": "", "frequency": "", "instructions": rest.strip()}


def mock_summary(facts: dict) -> dict:
    notes = _note_sections(facts.get("clinical_notes") or "")
    diagnosis = facts.get("diagnosis") or "Not documented"
    patient = facts.get("patient", "[PATIENT_1]")
    course = notes.get("course") or f"{patient} was admitted with {diagnosis.lower()}."
    return {
        "hospital_course": course,
        "discharge_diagnoses": [diagnosis],
        "procedures": _split(notes.get("procedures", "")),
        "medications": [_mock_medication(m) for m in _split(notes.get("medications", ""))],
        "activity": notes.get("activity", "As tolerated."),
        "diet": notes.get("diet", "Resume usual diet."),
        "follow_up": _split(notes.get("follow_up", "")) or ["Follow up with primary care as directed."],
        "warning_signs": _split(notes.get("warning_signs", "")) or [
            "Return to the emergency department for worsening symptoms."],
    }


# The assistant mock routes by phrase (first match wins), like the keyword bot it replaces.
MOCK_ROUTES: list[tuple[tuple[str, ...], str]] = [
    (("create task", "add task", "new task", "assign task", "create a task"), "create_task"),
    (("submit claim", "new claim", "file claim"), "submit_claim"),
    (("start discharge", "initiate discharge", "begin discharge"), "initiate_discharge"),
    (("list patients", "show patients", "all patients", "patients"), "list_patients"),
    (("my tasks", "list tasks", "show tasks"), "list_my_tasks"),
    (("analytics", "stats", "statistics", "dashboard"), "get_dashboard_stats"),
    (("claims", "insurance", "billing"), "list_claims"),
]
MOCK_HELP = (
    "I can help you with:\n"
    "• **List patients** and look up a patient\n"
    "• **My tasks** for your role\n"
    "• **Analytics** for a quick snapshot\n"
    "• **Insurance claims**\n"
    "• **Create a task**, e.g. 'create a task for [patient] to give meds'\n"
    "• **Start discharge** for a patient\n"
    "Anything that changes data needs your confirmation first."
)
TOKEN_RE = re.compile(r"\[PATIENT_\d+\]")


def _mock_tool_input(tool: str, text: str) -> dict:
    patient = TOKEN_RE.search(text)
    ref = patient.group(0) if patient else ""
    lower = text.lower()
    if tool == "create_task":
        role = "physician" if ("physician" in lower or "doctor" in lower) else (
            "pharmacist" if "pharmac" in lower else "nurse")
        idx = lower.find(" to ")
        title = text[idx + 4:].strip().rstrip(".").capitalize() if idx != -1 else "New task"
        return {"patient_ref": ref, "title": title, "description": text[:500], "assigned_role": role, "priority": 2}
    if tool == "initiate_discharge":
        return {"patient_ref": ref}
    if tool == "submit_claim":
        amount = re.search(r"\$?([\d,]+(?:\.\d+)?)", text.split("amount", 1)[-1])
        insurer = re.search(r"insurer(?: name)?:\s*([^\n]+)", text, re.I)
        return {"patient_ref": ref, "insurer_name": insurer.group(1).strip() if insurer else "",
                "policy_number": "", "billed_amount": float(amount.group(1).replace(",", "")) if amount else 0.0,
                "diagnosis_codes": [], "procedure_codes": []}
    return {}


def _mock_describe(result: str) -> str:
    """Restate a tool result in one line, without adding anything that is not in it."""
    try:
        data = json.loads(result)
    except ValueError:
        return result
    if isinstance(data, dict) and data.get("proposal") == "created":
        return "I've prepared that change. Review it below and confirm to apply it; nothing has changed yet."
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])
    if isinstance(data, dict) and "items" in data:
        items = data["items"]
        labels = [str(i.get("patient") or i.get("title") or i.get("insurer_name") or "") for i in items[:5]]
        more = "..." if len(items) > 5 else "."
        return f"Found {data.get('count', len(items))} result(s): {', '.join(l for l in labels if l)}{more}"
    return ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in data.items()) if isinstance(data, dict) else result


class MockLLMClient:
    provider = "mock"

    def __init__(self, model_id: str = "mock-1"):
        self.model_id = model_id

    async def complete(self, *, system, messages, max_tokens, tools=None, json_schema=None) -> LLMResponse:
        last = messages[-1]
        content = last["content"]
        if json_schema is not None:
            match = FACTS_RE.search(content if isinstance(content, str) else json.dumps(content))
            facts = json.loads(match.group(1)) if match else {}
            return self._reply(json.dumps(mock_summary(facts)), messages)

        if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
            lines = []
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                inner = block["content"]
                inner = TOOL_DATA_RE.sub(r"\1", inner) if isinstance(inner, str) else str(inner)
                lines.append(_mock_describe(inner.strip()))
            return self._reply("\n".join(lines), messages)

        text = content if isinstance(content, str) else ""
        lower = text.lower()
        names = {t["name"] for t in tools or []}
        if "help" in lower or "what can you do" in lower:
            return self._reply(MOCK_HELP, messages)
        for phrases, tool in MOCK_ROUTES:
            if tool in names and any(p in lower for p in phrases):
                call = ToolCall(f"toolu_mock_{len(messages)}", tool, _mock_tool_input(tool, text))
                return self._reply("", messages, [call])
        return self._reply("I didn't quite understand that. Try 'list patients', 'my tasks', or 'help'.", messages)

    def _reply(self, text: str, messages: list[dict], calls: list[ToolCall] | None = None) -> LLMResponse:
        calls = calls or []
        content: list[dict] = [{"type": "text", "text": text}] if text else []
        content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        prompt_chars = sum(len(json.dumps(m["content"])) for m in messages)
        return LLMResponse(text=text, tool_calls=calls, stop_reason="tool_use" if calls else "end_turn",
                           model_id=self.model_id, provider=self.provider, input_tokens=prompt_chars // 4,
                           output_tokens=len(text) // 4, latency_ms=0, content=content)


TOOL_DATA_RE = re.compile(r"<tool_data[^>]*>(.*?)</tool_data>", re.S)


# ---------- factory ----------

def _bedrock_model(model_id: str) -> str:
    return model_id if model_id.startswith("anthropic.") else f"anthropic.{model_id}"


def get_llm_client(purpose: Purpose) -> LLMClient:
    """The configured client for `purpose`. Raises 503 when AI is disabled or misconfigured."""
    provider = settings.llm_provider
    model = settings.summary_model if purpose == "summary" else settings.assistant_model
    effort = "medium" if purpose == "summary" else "low"
    if provider == "mock":
        return MockLLMClient()
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise HTTPException(status_code=503, detail="AI provider not configured (ANTHROPIC_API_KEY)")
        import anthropic

        return ClaudeClient(anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=60.0),
                            "anthropic", model, effort)
    if provider == "bedrock":
        if not settings.aws_region:
            raise HTTPException(status_code=503, detail="AI provider not configured (AWS_REGION)")
        import anthropic

        return ClaudeClient(anthropic.AsyncAnthropicBedrockMantle(aws_region=settings.aws_region, timeout=60.0),
                            "bedrock", _bedrock_model(model), effort)
    raise HTTPException(status_code=503, detail="AI provider not configured")

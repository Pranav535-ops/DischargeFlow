"""'Explain this trend': narrative insights over aggregate discharge metrics (ADR-001 Phase 5, item 20).

The model sees only analytics.service.discharge_aggregates output (counts and durations by role, no PHI), so no
de-identification is needed. Each finding must cite a metric path that exists in that input; findings citing
anything else are dropped and reported, so the panel never shows a claim the numbers don't support.
"""
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.ai.llm import LLMClient, LLMError
from app.ai.prompts import get_prompt
from app.ai.schemas import inline_refs
from app.analytics.service import metric_paths

MAX_TOKENS = 3000


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    detail: str
    metric: str = Field(description="Path of the metric this finding is based on, exactly as in <aggregates>")
    suggestion: str


class InsightsDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str
    findings: list[Finding] = Field(max_length=5)


INSIGHTS_JSON_SCHEMA = inline_refs(InsightsDoc.model_json_schema())


async def generate_insights(client: LLMClient, aggregates: dict) -> tuple[dict, InsightsDoc | None, list[str]]:
    """Returns (provenance fields, insights or None, citations that were rejected)."""
    prompt = get_prompt("insights")
    user_msg = prompt.render_user(aggregates_json=json.dumps(aggregates, indent=2, sort_keys=True))
    prov = {"provider": client.provider, "model_id": client.model_id, "prompt_version": prompt.id,
            "prompt_sha256": prompt.sha256, "attempts": 1, "status": "failed", "error": None,
            "input_hash": hashlib.sha256(f"{prompt.id}\n{user_msg}".encode("utf-8")).hexdigest(),
            "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
    try:
        resp = await client.complete(system=prompt.system, messages=[{"role": "user", "content": user_msg}],
                                     max_tokens=MAX_TOKENS, json_schema=INSIGHTS_JSON_SCHEMA)
    except LLMError as e:
        prov["error"] = str(e)[:500]
        return prov, None, []
    prov.update(model_id=resp.model_id, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                latency_ms=resp.latency_ms)
    try:
        doc = InsightsDoc.model_validate_json(resp.text)
    except ValidationError as e:
        prov["status"], prov["error"] = "invalid_output", str(e)[:500]
        return prov, None, []

    known = metric_paths(aggregates)
    rejected = [f.metric for f in doc.findings if f.metric not in known]
    doc.findings = [f for f in doc.findings if f.metric in known]
    prov["status"] = "succeeded"
    prov["grounding_warnings"] = [{"type": "metric", "value": m, "reason": "cited metric not in the input"}
                                  for m in rejected]
    return prov, doc, rejected


def mock_insights(aggregates: dict) -> dict:
    """Deterministic insights for the mock adapter: restates the largest numbers it is given."""
    findings = []
    by_role = {r: s for r, s in aggregates.get("task_completion_time_by_role", {}).items() if "avg_hours" in s}
    if by_role:
        role, stats = max(by_role.items(), key=lambda kv: kv[1]["avg_hours"])
        findings.append({"title": f"{role.capitalize()} tasks take longest",
                         "detail": f"{role} tasks average {stats['avg_hours']} h from creation to completion "
                                   f"(n={stats['n']}).",
                         "metric": f"task_completion_time_by_role.{role}.avg_hours",
                         "suggestion": f"Review the {role} queue and handoffs for discharge tasks."})
    open_by_role = aggregates.get("open_tasks_by_role", {})
    if open_by_role:
        role, stats = max(open_by_role.items(), key=lambda kv: kv[1]["n"])
        findings.append({"title": f"Most open tasks sit with {role}s",
                         "detail": f"{stats['n']} open tasks are assigned to the {role} role.",
                         "metric": f"open_tasks_by_role.{role}.n",
                         "suggestion": "Check staffing against the open task count."})
    ai = aggregates.get("ai_summaries", {})
    if ai.get("avg_edit_similarity") is not None:
        findings.append({"title": "Physician edits to AI drafts",
                         "detail": f"Approved drafts are {round(ai['avg_edit_similarity'] * 100)}% similar to the "
                                   "AI text on average.",
                         "metric": "ai_summaries.avg_edit_similarity",
                         "suggestion": "Sample the most-edited drafts when evaluating the next prompt version."})
    headline = findings[0]["title"] if findings else "Not enough completed discharges yet to identify trends."
    return {"headline": headline, "findings": findings}

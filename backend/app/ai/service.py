"""Governed discharge-summary generation (ADR-001 Phase 3).

Pipeline: collect only the facts the prompt needs -> de-identify -> render the versioned prompt -> ask the model for
JSON matching DischargeSummaryDoc (one retry on invalid output, then a visible failure) -> check grounding ->
re-identify -> store a draft plus a provenance row. A physician approves (and may edit) every draft; the edit diff
is stored as the main quality signal.
"""
import difflib
import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.deid import Deidentifier, age_from_dob, days_between
from app.ai.grounding import check_grounding
from app.ai.llm import LLMClient, LLMError, get_llm_client
from app.ai.models import AIGeneration, DischargeSummary
from app.ai.prompts import get_prompt
from app.ai.schemas import SUMMARY_JSON_SCHEMA, DischargeSummaryDoc, render_summary
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.discharge.models import Task, Workflow
from app.patients.models import Patient

MAX_ATTEMPTS = 2  # the first try plus one retry on invalid output
SUMMARY_MAX_TOKENS = 8000
MAX_NOTES_CHARS = 20000


def summary_facts(patient: Patient, tasks: list[Task], clinical_notes: str, deid: Deidentifier) -> dict:
    """The minimum the prompt needs, already de-identified. No DOB, dates, room or ids are sent."""
    patient_tok = deid.add_person("PATIENT", patient.name)
    physician_tok = deid.add_person("PHYSICIAN", patient.attending_physician, match_parts=False)
    return {
        "patient": patient_tok,
        "age_years": age_from_dob(patient.date_of_birth),
        "length_of_stay_days": days_between(patient.admission_date),
        "attending_physician": physician_tok,
        "diagnosis": deid.redact(patient.diagnosis),
        "completed_discharge_tasks": [deid.redact(t.title) for t in tasks if t.status == "completed"],
        "open_discharge_tasks": [deid.redact(t.title) for t in tasks if t.status != "completed"],
        "clinical_notes": deid.redact(clinical_notes[:MAX_NOTES_CHARS]),
    }


@dataclass
class SummaryResult:
    generation: Optional[AIGeneration]
    doc: Optional[DischargeSummaryDoc]  # re-identified; None when generation failed
    content: Optional[str]
    error: Optional[str] = None


async def run_summary_model(client: LLMClient, facts: dict, deid: Deidentifier) -> tuple[dict, SummaryResult]:
    """Call the model (with one retry on invalid output). Returns provenance fields and the result.

    Separate from persistence so the golden-set evals exercise exactly this code path.
    """
    prompt = get_prompt("discharge_summary")
    user_msg = prompt.render_user(facts_json=json.dumps(facts, indent=2, sort_keys=True))
    input_hash = hashlib.sha256(f"{prompt.id}\n{prompt.system}\n{user_msg}".encode("utf-8")).hexdigest()
    prov = {"provider": client.provider, "model_id": client.model_id, "prompt_version": prompt.id,
            "prompt_sha256": prompt.sha256, "input_hash": input_hash, "input_tokens": 0, "output_tokens": 0,
            "latency_ms": 0, "attempts": 0, "grounding_warnings": []}

    doc, error, message = None, None, user_msg
    for _ in range(MAX_ATTEMPTS):
        prov["attempts"] += 1
        try:
            resp = await client.complete(system=prompt.system, messages=[{"role": "user", "content": message}],
                                         max_tokens=SUMMARY_MAX_TOKENS, json_schema=SUMMARY_JSON_SCHEMA)
        except LLMError as e:
            prov["status"], prov["error"] = "failed", str(e)[:500]
            return prov, SummaryResult(generation=None, doc=None, content=None, error=prov["error"])
        prov["model_id"] = resp.model_id
        prov["input_tokens"] += resp.input_tokens
        prov["output_tokens"] += resp.output_tokens
        prov["latency_ms"] += resp.latency_ms
        try:
            doc = DischargeSummaryDoc.model_validate_json(resp.text)
            break
        except ValidationError as e:
            error = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:10])
            message = (f"{user_msg}\n\nA previous attempt returned output that failed validation ({error}). "
                       "Return only JSON that matches the schema exactly.")
    if doc is None:
        prov["status"], prov["error"] = "invalid_output", f"invalid output after {MAX_ATTEMPTS} attempts: {error}"[:500]
        return prov, SummaryResult(generation=None, doc=None, content=None, error=prov["error"])

    # Grounding is checked on the de-identified text, against exactly what the model was given.
    prov["grounding_warnings"] = check_grounding(doc, json.dumps(facts))
    prov["status"], prov["error"] = "succeeded", None
    final = DischargeSummaryDoc.model_validate(deid.reidentify_obj(doc.model_dump()))
    return prov, SummaryResult(generation=None, doc=final, content=render_summary(final))


async def generate_discharge_summary(db: AsyncSession, user: CurrentUser, patient: Patient, workflow: Workflow,
                                     clinical_notes: str = "") -> SummaryResult:
    """Generate and store a draft. On failure the provenance row is still added (the caller commits it)."""
    client = get_llm_client("summary")
    tasks = list(await db.scalars(select(Task).where(Task.workflow_id == workflow.workflow_id)))
    deid = Deidentifier()
    facts = summary_facts(patient, tasks, clinical_notes, deid)
    prov, result = await run_summary_model(client, facts, deid)

    generation = AIGeneration(purpose="discharge_summary", user_id=user.user_id, workflow_id=workflow.workflow_id,
                              patient_id=patient.patient_id, **prov)
    db.add(generation)
    await db.flush()
    result.generation = generation
    if result.doc is None:
        return result

    db.add(DischargeSummary(
        workflow_id=workflow.workflow_id, patient_id=patient.patient_id, content=result.content,
        original_content=result.content, structured=result.doc.model_dump(), generation_id=generation.generation_id,
        generated_by="ai" if client.provider != "mock" else "mock",
    ))
    await db.flush()
    return result


def edit_diff(original: str, edited: str) -> tuple[str, float]:
    diff = "\n".join(difflib.unified_diff(original.splitlines(), edited.splitlines(), "ai_draft", "approved",
                                          lineterm=""))
    return diff, round(difflib.SequenceMatcher(None, original, edited).ratio(), 4)


def apply_approval(summary: DischargeSummary, user: CurrentUser, edited_content: Optional[str]) -> bool:
    """Approve the draft, recording the physician's edits. Returns whether the text was changed."""
    original = summary.original_content or summary.content
    final = edited_content if edited_content is not None and edited_content.strip() else summary.content
    summary.edit_diff, summary.edit_similarity = edit_diff(original, final)
    changed = final != original
    if changed:
        summary.content = final
    summary.approved = True
    summary.approved_by = user.user_id
    summary.approved_at = utcnow()
    return changed


async def quality_metrics(db: AsyncSession) -> list[dict]:
    """Per prompt version: volume, failures, approval rate and how much physicians edited (ADR-001 observability)."""
    rows = await db.execute(
        select(
            AIGeneration.prompt_version,
            AIGeneration.model_id,
            func.count(AIGeneration.generation_id),
            func.sum(case((AIGeneration.status != "succeeded", 1), else_=0)),
            func.count(DischargeSummary.approved_at),
            func.avg(DischargeSummary.edit_similarity),
            func.avg(AIGeneration.latency_ms),
            func.sum(AIGeneration.input_tokens),
            func.sum(AIGeneration.output_tokens),
        )
        .outerjoin(DischargeSummary, DischargeSummary.generation_id == AIGeneration.generation_id)
        .where(AIGeneration.purpose == "discharge_summary")
        .group_by(AIGeneration.prompt_version, AIGeneration.model_id)
        .order_by(AIGeneration.prompt_version)
    )
    out = []
    for version, model, total, failed, approved, similarity, latency, tok_in, tok_out in rows:
        out.append({
            "prompt_version": version, "model_id": model, "generations": total, "failed": int(failed or 0),
            "approved": approved, "approval_rate": round(approved / total, 3) if total else None,
            "avg_edit_similarity": round(float(similarity), 4) if similarity is not None else None,
            "avg_latency_ms": round(float(latency)) if latency is not None else None,
            "input_tokens": int(tok_in or 0), "output_tokens": int(tok_out or 0),
        })
    return out

"""Patient-facing discharge instructions (ADR-002 item 18).

Pipeline, on the same governed path as summaries (ai/service.py): take the SIGNED summary only (its latest signed
version plus signed addenda) -> de-identify -> render the versioned prompt with the target language and reading grade
-> ask for JSON matching PatientInstructionsDoc (one retry on invalid output, or on English text above the target
grade) -> check grounding against the signed text -> re-identify -> measure readability -> store a draft plus a
provenance row. A nurse or physician edits and approves the draft before it can be printed.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.deid import Deidentifier
from app.ai.grounding import check_instructions_grounding
from app.ai.llm import LLMClient, LLMError, get_llm_client
from app.ai.models import AIGeneration, DischargeSummary, PatientInstructions, content_sha256
from app.ai.prompts import get_prompt
from app.ai.readability import grade_for, is_english
from app.ai.schemas import INSTRUCTIONS_JSON_SCHEMA, PatientInstructionsDoc, render_instructions, summary_sections
from app.ai.service import edit_diff
from app.core.config import settings
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.patients.models import Patient

MAX_ATTEMPTS = 2  # the first try plus one retry (invalid output, or English text above the target grade)
INSTRUCTIONS_MAX_TOKENS = 8000
DEFAULT_LANGUAGE = "en"
# Names sent with the code so the model (and the reviewer's language picker) need not guess. Any valid BCP 47 code
# works; codes not listed here are sent as they are.
LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "zh": "Chinese", "vi": "Vietnamese", "tl": "Tagalog", "ar": "Arabic",
    "fr": "French", "ko": "Korean", "ru": "Russian", "pt": "Portuguese", "ht": "Haitian Creole", "hi": "Hindi",
    "pl": "Polish", "de": "German", "it": "Italian", "ja": "Japanese", "fa": "Persian", "ur": "Urdu",
    "bn": "Bengali", "so": "Somali",
}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower().split("-", 1)[0], code)


def signed_source(summary: DischargeSummary) -> tuple[str, list[str], Optional[str]]:
    """(signed text, signed addenda texts, version id): the only input instructions are drafted from."""
    version = summary.latest_version
    text = version.content if version is not None else summary.content  # legacy: signed before migration 0013
    return text, [a.content for a in summary.addenda_for(version)], version.version_id if version else None


def source_hash(text: str, addenda: list[str]) -> str:
    return content_sha256(json.dumps([text, addenda]))


def instruction_facts(patient: Patient, summary_text: str, addenda: list[str], language: str,
                      deid: Deidentifier) -> dict:
    """The minimum the prompt needs, already de-identified: the signed text and how to write for this patient."""
    deid.add_person("PATIENT", patient.name)
    deid.add_person("PHYSICIAN", patient.attending_physician, match_parts=False)
    return {
        "language": language,
        "language_name": language_name(language),
        "target_reading_grade": settings.instructions_target_grade,
        "signed_summary": deid.redact(summary_text),
        "addenda": [deid.redact(a) for a in addenda],
    }


@dataclass
class InstructionsResult:
    doc: Optional[PatientInstructionsDoc]  # re-identified; None when generation failed
    content: Optional[str]
    reading_grade: Optional[float] = None
    error: Optional[str] = None
    generation: Optional[AIGeneration] = None


async def run_instructions_model(client: LLMClient, facts: dict, deid: Deidentifier) -> tuple[dict, InstructionsResult]:
    """Call the model (one retry). Returns provenance fields and the result. The evals run exactly this function."""
    prompt = get_prompt("patient_instructions")
    user_msg = prompt.render_user(facts_json=json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False))
    input_hash = hashlib.sha256(f"{prompt.id}\n{prompt.system}\n{user_msg}".encode("utf-8")).hexdigest()
    prov = {"provider": client.provider, "model_id": client.model_id, "prompt_version": prompt.id,
            "prompt_sha256": prompt.sha256, "input_hash": input_hash, "input_tokens": 0, "output_tokens": 0,
            "latency_ms": 0, "attempts": 0, "grounding_warnings": []}
    language, target = facts["language"], facts["target_reading_grade"]

    doc, error, message = None, None, user_msg
    for _ in range(MAX_ATTEMPTS):
        prov["attempts"] += 1
        try:
            resp = await client.complete(system=prompt.system, messages=[{"role": "user", "content": message}],
                                         max_tokens=INSTRUCTIONS_MAX_TOKENS, json_schema=INSTRUCTIONS_JSON_SCHEMA)
        except LLMError as e:
            prov["status"], prov["error"] = "failed", str(e)[:500]
            return prov, InstructionsResult(doc=None, content=None, error=prov["error"])
        prov["model_id"] = resp.model_id
        prov["input_tokens"] += resp.input_tokens
        prov["output_tokens"] += resp.output_tokens
        prov["latency_ms"] += resp.latency_ms
        try:
            candidate = PatientInstructionsDoc.model_validate_json(resp.text)
        except ValidationError as e:
            error = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:10])
            message = (f"{user_msg}\n\nA previous attempt returned output that failed validation ({error}). "
                       "Return only JSON that matches the schema exactly.")
            continue
        doc = candidate
        grade = grade_for(render_instructions(doc), language)
        if grade is None or grade <= target:
            break
        # Valid but too hard to read: keep it (in case the retry is worse) and ask once for simpler text.
        message = (f"{user_msg}\n\nA previous draft read at grade {grade:g}, above the target of {target:g}. "
                   "Use shorter sentences and simpler words. Keep every fact; add none.")
    if doc is None:
        prov["status"], prov["error"] = "invalid_output", f"invalid output after {MAX_ATTEMPTS} attempts: {error}"[:500]
        return prov, InstructionsResult(doc=None, content=None, error=prov["error"])

    # Grounding is checked on the de-identified text, against exactly the signed text the model was given.
    source = "\n\n".join([facts["signed_summary"], *facts["addenda"]])
    warnings = check_instructions_grounding(doc, source)
    final = PatientInstructionsDoc.model_validate(deid.reidentify_obj(doc.model_dump()))
    content = render_instructions(final)
    grade = grade_for(content, language)
    if grade is not None and grade > target:
        warnings.append({"type": "readability", "value": f"grade {grade:g}",
                         "reason": f"reads above the target grade of {target:g}; simplify before approving"})
    prov["grounding_warnings"] = warnings
    prov["status"], prov["error"] = "succeeded", None
    return prov, InstructionsResult(doc=final, content=content, reading_grade=grade)


async def generate(db: AsyncSession, user: CurrentUser, summary: DischargeSummary,
                   language: str) -> tuple[InstructionsResult, Optional[PatientInstructions]]:
    """Draft instructions from a signed summary. On failure the provenance row is still added (the caller commits).

    An unapproved draft in the same language is replaced; approved instructions are not (409)."""
    if not summary.approved:
        raise HTTPException(status_code=409, detail="Sign the discharge summary first: instructions are written "
                                                    "from the signed summary only")
    patient = summary.patient
    if patient.gdpr_erased:
        raise HTTPException(status_code=400, detail="The patient's record was erased")
    existing = await db.scalar(select(PatientInstructions).where(
        PatientInstructions.workflow_id == summary.workflow_id, PatientInstructions.language == language))
    if existing is not None and existing.approved:
        raise HTTPException(status_code=409, detail="Instructions in this language are already approved")

    client = get_llm_client("instructions")
    text, addenda, version_id = signed_source(summary)
    deid = Deidentifier()
    facts = instruction_facts(patient, text, addenda, language, deid)
    prov, result = await run_instructions_model(client, facts, deid)

    generation = AIGeneration(purpose="patient_instructions", user_id=user.user_id, workflow_id=summary.workflow_id,
                              patient_id=patient.patient_id, **prov)
    db.add(generation)
    await db.flush()
    result.generation = generation
    if result.doc is None:
        return result, None

    if existing is not None:
        await db.delete(existing)
        await db.flush()
    row = PatientInstructions(
        # `patient` first: the text fields are encrypted with its data key as they are set.
        patient=patient, patient_id=patient.patient_id, workflow_id=summary.workflow_id, summary_id=summary.summary_id,
        source_version_id=version_id, source_sha256=source_hash(text, addenda), language=language,
        content=result.content, original_content=result.content, structured=result.doc.model_dump(),
        reading_grade=result.reading_grade, target_grade=facts["target_reading_grade"],
        generation_id=generation.generation_id, generated_by="ai" if client.provider != "mock" else "mock",
        created_by=user.user_id,
    )
    db.add(row)
    await db.flush()
    return result, row


def approve(instructions: PatientInstructions, user: CurrentUser, edited_content: Optional[str]) -> bool:
    """Approve the draft, recording the reviewer's edits. Returns whether the text was changed."""
    if instructions.approved:
        raise HTTPException(status_code=409, detail="Instructions already approved")
    if instructions.patient.gdpr_erased:
        raise HTTPException(status_code=400, detail="The patient's record was erased")
    original = instructions.original_content or instructions.content
    final = edited_content if edited_content is not None and edited_content.strip() else instructions.content
    instructions.edit_diff, instructions.edit_similarity = edit_diff(original, final)
    changed = final != original
    if changed:
        instructions.content = final
    instructions.approved_reading_grade = grade_for(final, instructions.language)
    instructions.approved = True
    instructions.approved_by = user.user_id
    instructions.approved_at = utcnow()
    return changed


def view(instructions: PatientInstructions, summary: Optional[DischargeSummary],
         generation: Optional[AIGeneration] = None) -> dict:
    """The API shape: the row (decrypted), the language name, whether the signed source changed since (an addendum
    came later), and the draft's provenance and warnings."""
    out = instructions.to_dict()
    out["language_name"] = language_name(instructions.language)
    out["english_readability"] = is_english(instructions.language)
    stale = False
    if summary is not None and not instructions.patient.gdpr_erased:
        text, addenda, _ = signed_source(summary)
        stale = source_hash(text, addenda) != instructions.source_sha256
    out["stale"] = stale
    out["generation"] = generation.to_dict() if generation is not None else None
    return out


# ---------- mock (offline, deterministic) ----------

MOCK_HEADINGS = {"what_happened": "Why you were in the hospital", "medicines": "Your medicines",
                 "warning_signs": "Get help right away if", "follow_up": "Your follow-up visits",
                 "activity_and_diet": "Activity and food"}


def _bullets(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-").strip()
        if line and line.lower() not in ("none documented", "not documented"):
            out.append(line)
    return out


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else f"{text}."


def mock_instructions(facts: dict) -> dict:
    """What the mock adapter returns for a patient_instructions prompt. Like the summary mock, it only restates what
    the signed summary says (by its section layout), never copies free prose (which could carry injected text), and
    writes English whatever language was asked for: it can't translate."""
    sections = summary_sections(facts.get("signed_summary", ""))
    diagnoses = _bullets(sections.get("discharge diagnoses", ""))
    medicines = []
    for entry in _bullets(sections.get("discharge medications", "")):
        name, _, rest = entry.partition(" ")
        rest = rest.strip().strip("()").strip()
        medicines.append({"name": name, "how_to_take": _sentence(f"Take {rest}") if rest else
                          "Take it as your care team told you.", "purpose": ""})
    activity, diet = sections.get("activity", ""), sections.get("diet", "")
    return {
        "headings": dict(MOCK_HEADINGS),
        "what_happened": ("You were in the hospital for " + (diagnoses[0].lower() if diagnoses else "care")
                          + ". Your care team treated you. You are ready to go home."),
        "conditions": [{"medical_term": d, "plain_explanation": "Ask your care team what this means for you."}
                       for d in diagnoses],
        "medicines": medicines,
        "warning_signs": [_sentence(w) for w in _bullets(sections.get("warning signs (return to care if)", ""))]
        or ["Call your doctor if you feel worse."],
        "follow_up": [_sentence(f) for f in _bullets(sections.get("follow-up", ""))],
        "activity_and_diet": " ".join(_sentence(t) for t in (activity, diet)
                                      if t and t.lower() != "not documented") or "Ask your care team.",
    }

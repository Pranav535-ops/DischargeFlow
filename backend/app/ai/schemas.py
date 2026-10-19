"""The structured discharge summary the model must return, and how it is rendered for the record."""
import copy

from pydantic import BaseModel, ConfigDict, Field


class Medication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    dose: str
    frequency: str
    instructions: str


class DischargeSummaryDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hospital_course: str = Field(min_length=1)
    discharge_diagnoses: list[str] = Field(min_length=1)
    procedures: list[str]
    medications: list[Medication]
    activity: str
    diet: str
    follow_up: list[str]
    warning_signs: list[str]


ANNOTATIONS = ("title", "minLength", "minItems", "maxLength", "maxItems", "minimum", "maximum")


def inline_refs(schema: dict) -> dict:
    """Model-facing schemas are simplest without $ref; inline the (non-recursive) definitions.

    Length and range constraints are dropped from the wire schema and enforced by Pydantic validation instead.
    """
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
            # A property may itself be called "title"; only drop annotation values, never property schemas.
            return {k: walk(v) for k, v in node.items() if not (k in ANNOTATIONS and not isinstance(v, dict))}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


SUMMARY_JSON_SCHEMA = inline_refs(DischargeSummaryDoc.model_json_schema())


def render_summary(doc: DischargeSummaryDoc) -> str:
    """Plain-text document stored as the summary's `content` (what the physician reads and edits)."""

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- None documented"

    meds = [
        " ".join(p for p in (m.name, m.dose, m.frequency) if p) + (f" ({m.instructions})" if m.instructions else "")
        for m in doc.medications
    ]
    return "\n\n".join([
        "DISCHARGE SUMMARY",
        f"Hospital course:\n{doc.hospital_course}",
        f"Discharge diagnoses:\n{bullets(doc.discharge_diagnoses)}",
        f"Procedures:\n{bullets(doc.procedures)}",
        f"Discharge medications:\n{bullets(meds)}",
        f"Activity:\n{doc.activity or 'Not documented'}",
        f"Diet:\n{doc.diet or 'Not documented'}",
        f"Follow-up:\n{bullets(doc.follow_up)}",
        f"Warning signs (return to care if):\n{bullets(doc.warning_signs)}",
    ])


# ---------- Patient-facing discharge instructions (ADR-002 item 18) ----------

class InstructionHeadings(BaseModel):
    """Section headings, in the patient's language (the model writes them)."""
    model_config = ConfigDict(extra="forbid")

    what_happened: str = Field(min_length=1)
    medicines: str = Field(min_length=1)
    warning_signs: str = Field(min_length=1)
    follow_up: str = Field(min_length=1)
    activity_and_diet: str = Field(min_length=1)


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Copied exactly from the signed summary (checked against it), then explained in plain words.
    medical_term: str = Field(min_length=1)
    plain_explanation: str


class InstructionMedicine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The medicine's name exactly as the signed summary writes it (checked against it).
    name: str = Field(min_length=1)
    how_to_take: str
    purpose: str


class PatientInstructionsDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headings: InstructionHeadings
    what_happened: str = Field(min_length=1)
    conditions: list[Condition]
    medicines: list[InstructionMedicine]
    warning_signs: list[str] = Field(min_length=1)
    follow_up: list[str]
    activity_and_diet: str


INSTRUCTIONS_JSON_SCHEMA = inline_refs(PatientInstructionsDoc.model_json_schema())


def render_instructions(doc: PatientInstructionsDoc) -> str:
    """Plain-text handout stored as `content`: what the clinician reads, edits and approves, and what is printed."""
    h = doc.headings

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items)

    about = doc.what_happened + ("\n" + bullets([f"{c.medical_term}: {c.plain_explanation}" if c.plain_explanation
                                                 else c.medical_term for c in doc.conditions])
                                 if doc.conditions else "")
    meds = [" ".join(p for p in (f"{m.name}:", m.how_to_take, m.purpose) if p.strip()) for m in doc.medicines]
    sections = [(h.what_happened, about), (h.medicines, bullets(meds)), (h.warning_signs, bullets(doc.warning_signs)),
                (h.follow_up, bullets(doc.follow_up)), (h.activity_and_diet, doc.activity_and_diet)]
    return "\n\n".join(f"{title}\n{body}" for title, body in sections if body.strip())


def summary_sections(text: str) -> dict[str, str]:
    """Sections of a rendered discharge summary ("Heading:\nbody" blocks), keyed by lower-case heading.

    Signed text may have been edited by hand, so anything that doesn't follow the layout is simply not a section.
    """
    out: dict[str, str] = {}
    for block in (text or "").replace("\r\n", "\n").split("\n\n"):
        head, sep, body = block.partition("\n")
        if sep and head.rstrip().endswith(":"):
            out[head.rstrip()[:-1].strip().lower()] = body.strip()
    return out

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

"""The LACE index: risk of death or unplanned readmission within 30 days of discharge (ADR-002 item 20).

van Walraven C, Dhalla IA, Bell C, et al. Derivation and validation of an index to predict early death or unplanned
readmission after discharge from hospital to the community. CMAJ 2010;182(6):551-557. doi:10.1503/cmaj.091117

A published, validated score over structured data, so the flag is explainable and auditable: every point comes from
one of four components, each shown with the input it came from. It is deliberately not an LLM, and nothing here is
ever sent to one (the `.importlinter` contract `risk-not-for-models` keeps app.ai and app.chatbot from importing it).

    L  length of stay in days: <1 -> 0, 1 -> 1, 2 -> 2, 3 -> 3, 4-6 -> 4, 7-13 -> 5, >=14 -> 7
    A  acute (emergent) admission -> 3
    C  Charlson comorbidity index: 0 -> 0, 1 -> 1, 2 -> 2, 3 -> 3, >=4 -> 5
    E  emergency department visits in the 6 months before admission, 1 point each, at most 4

The score runs from 0 to 19 (the paper reports 2.0% expected risk at 0 and 43.7% at 19; C statistic 0.684). A score
of 10 or more is the conventional high-risk cut-off used by hospitals running LACE; it is a local policy choice, not
a figure from the derivation paper, so it is a constant here for clinical governance to confirm.

Pure functions only: no database, no settings, no other app module. app.risk.service gathers the inputs.
"""
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Bump when any band, weight or input rule changes: stored scores record the version they were computed with, and
# encounters scored under an older version are recomputed when next read.
SCORE_VERSION = "lace-2010.1"
CITATION = ("van Walraven C, Dhalla IA, Bell C, et al. Derivation and validation of an index to predict early death "
            "or unplanned readmission after discharge from hospital to the community. CMAJ 2010;182(6):551-557. "
            "doi:10.1503/cmaj.091117")
MAX_SCORE = 19
HIGH_RISK_THRESHOLD = 10
ED_LOOKBACK_DAYS = 182  # "the six months before admission"
ED_POINTS_CAP = 4
ACUTE_POINTS = 3

# HL7 v2 table 0007 admission type (PV1-4). Accident, emergency and urgent admissions are unplanned ("acute");
# elective, routine, newborn and labor and delivery are not.
ADMISSION_TYPES = {
    "A": "Accident",
    "E": "Emergency",
    "U": "Urgent",
    "C": "Elective",
    "R": "Routine",
    "L": "Labor and delivery",
    "N": "Newborn",
}
ACUTE_ADMISSION_TYPES = frozenset({"A", "E", "U"})

# Charlson comorbidities and their weights as listed on the LACE scoring worksheet. Each condition counts once; the
# sum is the Charlson index that the C component bands.
# UNVERIFIED in this change against the paper's full text (not retrievable here): clinical governance must confirm
# these weights against the worksheet the hospital uses before relying on the flag. Changing one bumps SCORE_VERSION.
COMORBIDITIES: dict[str, tuple[str, int]] = {
    "myocardial_infarction": ("Previous myocardial infarction", 1),
    "cerebrovascular_disease": ("Cerebrovascular disease", 1),
    "peripheral_vascular_disease": ("Peripheral vascular disease", 1),
    "diabetes_uncomplicated": ("Diabetes without complications", 1),
    "congestive_heart_failure": ("Congestive heart failure", 2),
    "diabetes_end_organ_damage": ("Diabetes with end-organ damage", 2),
    "chronic_pulmonary_disease": ("Chronic pulmonary disease", 2),
    "mild_liver_disease": ("Mild liver disease", 2),
    "any_tumor": ("Any tumor (including lymphoma or leukemia)", 2),
    "dementia": ("Dementia", 3),
    "connective_tissue_disease": ("Connective tissue disease", 3),
    "aids": ("AIDS", 4),
    "moderate_severe_liver_disease": ("Moderate or severe liver disease", 4),
    "metastatic_solid_tumor": ("Metastatic solid tumor", 6),
}
# Where a condition is recorded in two grades, only the worse one counts (as in the Charlson index).
SUPERSEDED_BY = {
    "diabetes_uncomplicated": "diabetes_end_organ_damage",
    "mild_liver_disease": "moderate_severe_liver_disease",
    "any_tumor": "metastatic_solid_tumor",
}


def length_of_stay_points(days: int) -> int:
    if days < 0:
        raise ValueError("length of stay can't be negative")
    if days < 1:
        return 0
    if days <= 3:
        return days
    if days <= 6:
        return 4
    if days <= 13:
        return 5
    return 7


def acuity_points(admission_type: Optional[str]) -> int:
    return ACUTE_POINTS if is_acute(admission_type) else 0


def is_acute(admission_type: Optional[str]) -> bool:
    return (admission_type or "").strip().upper() in ACUTE_ADMISSION_TYPES


def unknown_comorbidities(keys: Iterable[str]) -> list[str]:
    return sorted(set(keys) - COMORBIDITIES.keys())


def counted_comorbidities(keys: Iterable[str]) -> list[str]:
    """The recorded conditions that score: known keys, each once, the milder grade dropped when the worse is there."""
    present = set(keys) & COMORBIDITIES.keys()
    return [k for k in COMORBIDITIES if k in present and SUPERSEDED_BY.get(k) not in present]


def charlson_index(keys: Iterable[str]) -> int:
    return sum(COMORBIDITIES[k][1] for k in counted_comorbidities(keys))


def comorbidity_points(charlson: int) -> int:
    if charlson < 0:
        raise ValueError("the Charlson index can't be negative")
    return charlson if charlson <= 3 else 5


def ed_visit_points(visits: int) -> int:
    if visits < 0:
        raise ValueError("a visit count can't be negative")
    return min(visits, ED_POINTS_CAP)


def is_high_risk(score: int) -> bool:
    return score >= HIGH_RISK_THRESHOLD


@dataclass(frozen=True)
class LaceInputs:
    """None means not recorded: the component scores 0 and is listed in `missing`, so the total is a lower bound."""
    length_of_stay_days: Optional[int]
    admission_type: Optional[str]
    comorbidities: Optional[list[str]]
    ed_visits: Optional[int]


@dataclass(frozen=True)
class LaceScore:
    score: int
    length_of_stay: int
    acuity: int
    comorbidity: int
    ed_visits: int
    charlson_index: int
    missing: list[str] = field(default_factory=list)
    version: str = SCORE_VERSION

    @property
    def high_risk(self) -> bool:
        return is_high_risk(self.score)

    @property
    def complete(self) -> bool:
        return not self.missing


def score(inputs: LaceInputs) -> LaceScore:
    missing = []
    if inputs.length_of_stay_days is None:
        missing.append("length_of_stay")
    if inputs.admission_type is None:
        missing.append("acuity")
    if inputs.comorbidities is None:
        missing.append("comorbidity")
    if inputs.ed_visits is None:
        missing.append("ed_visits")
    charlson = charlson_index(inputs.comorbidities or [])
    parts = (length_of_stay_points(inputs.length_of_stay_days or 0), acuity_points(inputs.admission_type),
             comorbidity_points(charlson), ed_visit_points(inputs.ed_visits or 0))
    return LaceScore(score=sum(parts), length_of_stay=parts[0], acuity=parts[1], comorbidity=parts[2],
                     ed_visits=parts[3], charlson_index=charlson, missing=missing)

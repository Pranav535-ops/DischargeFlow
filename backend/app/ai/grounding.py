"""Grounding checks: does a generated summary only state medications and diagnoses found in its input?

Used twice: at generation time the warnings are stored with the draft and shown to the approving physician, and the
golden-set evals (ai/evals) fail CI when any case produces one. The checks are lexical on purpose: they are cheap,
deterministic and explainable, and they err towards flagging.
"""
import re
from typing import Sequence

from app.ai.schemas import DischargeSummaryDoc

# Common inpatient and discharge drugs. A lexicon hit in the output that is absent from the input is a hallucination,
# even when the model mentions it only in prose (hospital course, instructions).
DRUG_LEXICON = frozenset("""
acetaminophen albuterol allopurinol amiodarone amlodipine amoxicillin ampicillin apixaban aspirin atenolol
atorvastatin azithromycin budesonide bumetanide carvedilol cefazolin cefdinir ceftriaxone cephalexin ciprofloxacin
clopidogrel dexamethasone diltiazem digoxin doxycycline empagliflozin enoxaparin escitalopram famotidine fentanyl
fluticasone furosemide gabapentin glipizide heparin hydralazine hydrochlorothiazide hydrocodone ibuprofen insulin
ipratropium isosorbide ketorolac labetalol lactulose levetiracetam levofloxacin levothyroxine lisinopril loratadine
lorazepam losartan meropenem metformin methylprednisolone metoprolol metronidazole montelukast morphine naloxone
nitrofurantoin nitroglycerin omeprazole ondansetron oxycodone pantoprazole piperacillin potassium prednisone
pregabalin propranolol rivaroxaban rosuvastatin senna sertraline simvastatin spironolactone sulfamethoxazole
tamsulosin tiotropium tramadol trazodone vancomycin warfarin
""".split())
STOPWORDS = frozenset("""
acute chronic with without and the of left right mild moderate severe primary secondary history status post due
to type stage unspecified suspected likely possible resolved resolving improved improving episode new onset
""".split())
WORD_RE = re.compile(r"[a-z][a-z0-9-]+")


def _words(text: str) -> set[str]:
    return set(WORD_RE.findall(text.lower()))


def _significant(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= 4 and w not in STOPWORDS}


def summary_text(doc: DischargeSummaryDoc) -> str:
    meds = " ".join(f"{m.name} {m.dose} {m.frequency} {m.instructions}" for m in doc.medications)
    return " ".join([doc.hospital_course, *doc.discharge_diagnoses, *doc.procedures, meds, doc.activity, doc.diet,
                     *doc.follow_up, *doc.warning_signs])


def check_grounding(doc: DischargeSummaryDoc, source_text: str, allergies: Sequence[str] = ()) -> list[dict]:
    """Return one warning per ungrounded medication or diagnosis (empty list = grounded), and one per discharge
    medication that names a recorded allergy (`allergies`: the EHR's list, ADR-002 item 15)."""
    source = _words(source_text)
    warnings: list[dict] = []

    for med in doc.medications:
        drug = (WORD_RE.findall(med.name.lower()) or [""])[0]
        if drug and drug not in source:
            warnings.append({"type": "medication", "value": med.name,
                             "reason": "medication not found in the source record"})
    listed = {(WORD_RE.findall(m.name.lower()) or [""])[0] for m in doc.medications}
    for drug in sorted((_words(summary_text(doc)) & DRUG_LEXICON) - source - listed):
        warnings.append({"type": "medication", "value": drug, "reason": "medication mentioned but not in the source record"})

    for dx in doc.discharge_diagnoses:
        missing = _significant(dx) - source
        if missing:
            warnings.append({"type": "diagnosis", "value": dx,
                             "reason": f"terms not in the source record: {', '.join(sorted(missing))}"})
    allergens = {w for a in allergies for w in _significant(a)}
    for med in doc.medications:
        drug = (WORD_RE.findall(med.name.lower()) or [""])[0]
        if drug and drug in allergens:
            warnings.append({"type": "allergy", "value": med.name,
                             "reason": "medication matches a recorded allergy"})
    return warnings

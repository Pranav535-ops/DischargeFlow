"""Charlson comorbidity flags proposed from coded diagnoses (ADR-002 item 20, fed by item 15's EHR problem list).

Proposals only. The LACE score counts only the comorbidities a clinician has recorded on the plan
(`Encounter.comorbidities`, set through PUT /api/encounters/{id}/risk-inputs). Nothing here writes them: the plan
screen lists each proposal with the condition it came from, unchecked, and the clinician accepts it through the
normal save. Pure functions, no app module but app.risk.lace; never sent to a model (the `risk-not-for-models`
import-linter contract).

ICD-10 codes: the Charlson ICD-10 coding algorithm of
    Quan H, Sundararajan V, Halfon P, et al. Coding algorithms for defining comorbidities in ICD-9-CM and ICD-10
    administrative data. Med Care 2005;43(11):1130-1139. doi:10.1097/01.mlr.0000182534.19832.83
Every list below was checked, code by code, against the `charlson_icd10_quan` map of the R package `comorbidity`
(Gasparini A, J Open Source Softw 2018;3(23):648; data-raw/make-mapping.R), which encodes Quan's Table 1. The paper's
table itself was not retrievable here (not in PubMed Central), so the source check is against that encoding, not the
printed table. Codes are matched as prefixes after removing the dot ("I50" matches I50.9 and I50.810), exactly as
Quan lists them ("I50.x").

Quan wrote the algorithm for WHO ICD-10. In ICD-10-CM (what US EHRs send) the matching is the same where the codes
exist, with these known differences, none of which is papered over here:
  - Quan codes with no ICD-10-CM counterpart simply never match: F00, E14, I64, B21, B22, B24, E10.7/E11.7/E12.7/
    E13.7/E14.7 ("multiple complications"), and the E12 group.
  - ICD-10-CM codes outside Quan's lists are NOT proposed, even where a clinician would count them: E08/E09 (diabetes
    due to an underlying condition / drug-induced), C7A/C7B (neuroendocrine tumours), C86 (other T/NK-cell
    lymphomas), Z21 (asymptomatic HIV, deliberately not AIDS), F02.8x without G30, and similar. A clinician records
    those by hand. Adding any is a local extension of the published algorithm and needs clinical governance.
  - In ICD-10-CM, E1x.6 (e.g. E11.65 hyperglycaemia, E11.621 foot ulcer) is "other specified complications"; Quan
    counts E1x.6 as diabetes WITHOUT chronic complication, and so does this map.
Quan's three Charlson categories that the LACE worksheet does not list (peptic ulcer disease K25-K28,
hemiplegia/paraplegia, renal disease) are not proposed: LACE has no key for them (see app.risk.lace.COMORBIDITIES).
Quan's "AIDS/HIV" (B20-B24, symptomatic HIV disease) proposes LACE's `aids`.

SNOMED CT: there is no published Quan-equivalent SNOMED algorithm. SNOMED_CT below is a LOCAL mapping of a few common
parent concepts, by analogy with the ICD-10 categories, matched by exact concept id only (no subsumption: a child
concept such as a specific cancer that is not listed is not proposed). Each concept id and its display were checked
as active on the public FHIR terminology server (tx.fhir.org, CodeSystem/$lookup, 2026-10-20); the assignment of
each concept to a Charlson key is UNVERIFIED against any published source and needs clinical governance review
before relying on it. When a Condition carries an ICD-10 coding too, both are matched; the clinician sees the codes.
"""
from typing import Iterable, Optional

from app.risk import lace

CITATION = ("Quan H, Sundararajan V, Halfon P, et al. Coding algorithms for defining comorbidities in ICD-9-CM and "
            "ICD-10 administrative data. Med Care 2005;43(11):1130-1139. doi:10.1097/01.mlr.0000182534.19832.83")

ICD10_SYSTEMS = {
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD-10-CM",
    "http://hl7.org/fhir/sid/icd-10": "ICD-10",
}
SNOMED_SYSTEM = "http://snomed.info/sct"


def _r(letter: str, start: int, end: int) -> list[str]:
    """Three-character categories letter+start .. letter+end inclusive, e.g. _r("I", 60, 69) -> I60..I69."""
    return [f"{letter}{n:02d}" for n in range(start, end + 1)]


# Quan et al. 2005, Charlson, ICD-10: prefixes without the dot, by LACE key.
ICD10_PREFIXES: dict[str, tuple[str, ...]] = {
    "myocardial_infarction": ("I21", "I22", "I252"),
    "congestive_heart_failure": ("I099", "I110", "I130", "I132", "I255", "I420", "I425", "I426", "I427", "I428",
                                 "I429", "I43", "I50", "P290"),
    "peripheral_vascular_disease": ("I70", "I71", "I731", "I738", "I739", "I771", "I790", "I792", "K551", "K558",
                                    "K559", "Z958", "Z959"),
    "cerebrovascular_disease": ("G45", "G46", "H340", *_r("I", 60, 69)),
    "dementia": ("F00", "F01", "F02", "F03", "F051", "G30", "G311"),
    "chronic_pulmonary_disease": ("I278", "I279", *_r("J", 40, 47), *_r("J", 60, 67), "J684", "J701", "J703"),
    "connective_tissue_disease": ("M05", "M06", "M315", "M32", "M33", "M34", "M351", "M353", "M360"),
    "mild_liver_disease": ("B18", "K700", "K701", "K702", "K703", "K709", "K713", "K714", "K715", "K717", "K73",
                           "K74", "K760", "K762", "K763", "K764", "K768", "K769", "Z944"),
    "diabetes_uncomplicated": tuple(f"E1{t}{c}" for t in "01234" for c in "01689"),
    "diabetes_end_organ_damage": tuple(f"E1{t}{c}" for t in "01234" for c in "23457"),
    "any_tumor": (*_r("C", 0, 26), *_r("C", 30, 34), *_r("C", 37, 41), "C43", *_r("C", 45, 58), *_r("C", 60, 76),
                  *_r("C", 81, 85), "C88", *_r("C", 90, 97)),
    "moderate_severe_liver_disease": ("I850", "I859", "I864", "I982", "K704", "K711", "K721", "K729", "K765", "K766",
                                      "K767"),
    "metastatic_solid_tumor": ("C77", "C78", "C79", "C80"),
    "aids": ("B20", "B21", "B22", "B24"),
}

# LOCAL, UNVERIFIED assignment (see the module docstring); concept ids and displays checked on tx.fhir.org.
SNOMED_CT: dict[str, tuple[str, str]] = {
    "22298006": ("myocardial_infarction", "Myocardial infarction"),
    "1755008": ("myocardial_infarction", "Old myocardial infarction"),
    "42343007": ("congestive_heart_failure", "Congestive heart failure"),
    "84114007": ("congestive_heart_failure", "Heart failure"),
    "400047006": ("peripheral_vascular_disease", "Peripheral vascular disease"),
    "399957001": ("peripheral_vascular_disease", "Peripheral arterial occlusive disease"),
    "62914000": ("cerebrovascular_disease", "Cerebrovascular disease"),
    "230690007": ("cerebrovascular_disease", "Cerebrovascular accident"),
    "266257000": ("cerebrovascular_disease", "Transient cerebral ischemia"),
    "52448006": ("dementia", "Dementia"),
    "26929004": ("dementia", "Alzheimer's disease"),
    "13645005": ("chronic_pulmonary_disease", "Chronic obstructive pulmonary disease"),
    "195967001": ("chronic_pulmonary_disease", "Asthma"),
    "69896004": ("connective_tissue_disease", "Rheumatoid arthritis"),
    "55464009": ("connective_tissue_disease", "Systemic lupus erythematosus"),
    "89155008": ("connective_tissue_disease", "Systemic sclerosis"),
    "61977001": ("mild_liver_disease", "Chronic type B viral hepatitis"),
    "128302006": ("mild_liver_disease", "Chronic hepatitis C"),
    "19943007": ("mild_liver_disease", "Cirrhosis of liver"),
    "73211009": ("diabetes_uncomplicated", "Diabetes mellitus"),
    "44054006": ("diabetes_uncomplicated", "Type 2 diabetes mellitus"),
    "46635009": ("diabetes_uncomplicated", "Type 1 diabetes mellitus"),
    "127013003": ("diabetes_end_organ_damage", "Disorder of kidney due to diabetes mellitus"),
    "4855003": ("diabetes_end_organ_damage", "Retinopathy due to diabetes mellitus"),
    "230572002": ("diabetes_end_organ_damage", "Neuropathy due to diabetes mellitus"),
    # Broader than Quan's malignancy list (it includes non-melanoma skin cancer, C44, which Quan leaves out).
    "363346000": ("any_tumor", "Malignant neoplastic disease"),
    "254837009": ("any_tumor", "Malignant neoplasm of breast"),
    "399068003": ("any_tumor", "Malignant neoplasm of prostate"),
    "363406005": ("any_tumor", "Malignant neoplasm of colon"),
    "93143009": ("any_tumor", "Leukemia"),
    "118600007": ("any_tumor", "Malignant lymphoma"),
    "28670008": ("moderate_severe_liver_disease", "Esophageal varices"),
    "128462008": ("metastatic_solid_tumor", "Metastatic malignant neoplasm"),
    "62479008": ("aids", "AIDS"),
}

assert set(ICD10_PREFIXES) <= lace.COMORBIDITIES.keys()
assert {k for k, _ in SNOMED_CT.values()} <= lace.COMORBIDITIES.keys()


def normalize_icd10(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def icd10_keys(code: str) -> list[str]:
    """The Charlson keys an ICD-10 / ICD-10-CM code proposes (normally none or one)."""
    norm = normalize_icd10(code)
    if len(norm) < 3:
        return []
    return [key for key, prefixes in ICD10_PREFIXES.items() if norm.startswith(prefixes)]


def coding_keys(system: Optional[str], code: Optional[str]) -> list[str]:
    if not code:
        return []
    if system in ICD10_SYSTEMS:
        return icd10_keys(code)
    if system == SNOMED_SYSTEM:
        hit = SNOMED_CT.get(code.strip())
        return [hit[0]] if hit else []
    return []


def system_label(system: Optional[str]) -> str:
    if system == SNOMED_SYSTEM:
        return "SNOMED CT"
    return ICD10_SYSTEMS.get(system or "", "")


def propose(conditions: Iterable[dict]) -> list[dict]:
    """Proposed Charlson keys from conditions shaped {"name": str, "codings": [{"system", "code"}]}.

    One entry per key, in the LACE worksheet's order, each listing every condition and code it came from. Where a
    condition is proposed in two grades (e.g. mild and severe liver disease), both are listed: the clinician picks,
    and the score counts only the worse one anyway (lace.SUPERSEDED_BY)."""
    sources: dict[str, list[dict]] = {}
    for condition in conditions:
        for coding in condition.get("codings") or []:
            for key in coding_keys(coding.get("system"), coding.get("code")):
                entry = {"condition": condition.get("name") or "", "system": system_label(coding.get("system")),
                         "code": str(coding.get("code"))}
                if entry not in sources.setdefault(key, []):
                    sources[key].append(entry)
    return [{"key": key, "label": label, "weight": weight, "sources": sources[key]}
            for key, (label, weight) in lace.COMORBIDITIES.items() if key in sources]

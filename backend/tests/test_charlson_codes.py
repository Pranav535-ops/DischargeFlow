"""Charlson flags proposed from coded diagnoses (app.risk.charlson_codes): Quan et al. 2005 ICD-10 at range edges."""
import pytest

from app.risk import charlson_codes as cc
from app.risk import lace

ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
SCT = "http://snomed.info/sct"

MI, CHF, PVD, CVD = ("myocardial_infarction", "congestive_heart_failure", "peripheral_vascular_disease",
                     "cerebrovascular_disease")
CPD, CTD, MLD, SLD = ("chronic_pulmonary_disease", "connective_tissue_disease", "mild_liver_disease",
                      "moderate_severe_liver_disease")
DM, DMC, TUMOR, METS = ("diabetes_uncomplicated", "diabetes_end_organ_damage", "any_tumor", "metastatic_solid_tumor")


@pytest.mark.parametrize("code,key", [
    # Myocardial infarction: I21, I22, I25.2 (not the rest of I25).
    ("I21.4", MI), ("I21.A1", MI), ("I22.9", MI), ("I25.2", MI), ("I25.10", None), ("I20.9", None), ("I23.0", None),
    # Heart failure: I42.0 and I42.5-I42.9 (not I42.1-I42.4); I43, I50, I09.9, I11.0, I13.0, I13.2, I25.5, P29.0.
    ("I42.0", CHF), ("I42.1", None), ("I42.4", None), ("I42.5", CHF), ("I42.9", CHF), ("I43", CHF),
    ("I50.810", CHF), ("I09.9", CHF), ("I09.81", None), ("I11.0", CHF), ("I11.9", None), ("I13.0", CHF),
    ("I13.10", None), ("I13.2", CHF), ("I25.5", CHF), ("P29.0", CHF), ("P29.11", None),
    # Peripheral vascular: I70-I71, I73.1/.8/.9 (not I73.0), I77.1, I79.0/.2, K55.1/.8/.9 (not K55.0), Z95.8/.9.
    ("I70.213", PVD), ("I71.4", PVD), ("I72.0", None), ("I73.00", None), ("I73.1", PVD), ("I73.9", PVD),
    ("I77.1", PVD), ("I77.0", None), ("I79.1", None), ("K55.059", None), ("K55.1", PVD), ("K55.9", PVD),
    ("Z95.818", PVD), ("Z95.1", None),
    # Cerebrovascular: G45, G46, H34.0, I60-I69 inclusive.
    ("G45.9", CVD), ("G46.3", CVD), ("G47.33", None), ("H34.00", CVD), ("H34.10", None), ("I59", None),
    ("I60.9", CVD), ("I69.351", CVD),
    # Dementia: F00-F03, F05.1, G30, G31.1 (not G31.0 or G31.2).
    ("F01.50", "dementia"), ("F03.90", "dementia"), ("F04", None), ("F05.1", "dementia"), ("F05.0", None),
    ("G30.9", "dementia"), ("G31.1", "dementia"), ("G31.01", None), ("G31.2", None),
    # Chronic pulmonary: J40-J47, J60-J67, J68.4, J70.1, J70.3, I27.8, I27.9.
    ("J39.9", None), ("J40", CPD), ("J44.1", CPD), ("J45.909", CPD), ("J47.9", CPD), ("J48", None), ("J59", None),
    ("J60", CPD), ("J67.9", CPD), ("J68.0", None), ("J68.4", CPD), ("J70.0", None), ("J70.1", CPD), ("J70.3", CPD),
    ("I27.81", CPD), ("I27.9", CPD), ("I27.0", None),
    # Rheumatic (connective tissue): M05, M06, M31.5, M32-M34, M35.1, M35.3, M36.0.
    ("M05.79", CTD), ("M06.9", CTD), ("M07", None), ("M31.5", CTD), ("M31.6", None), ("M32.14", CTD),
    ("M34.9", CTD), ("M35.0", None), ("M35.1", CTD), ("M35.3", CTD), ("M36.0", CTD), ("M36.1", None),
    # Mild liver: B18, K70.0-K70.3, K70.9, K71.3-K71.5, K71.7, K73, K74, K76.0, K76.2-K76.4, K76.8, K76.9, Z94.4.
    ("B17.10", None), ("B18.2", MLD), ("K70.0", MLD), ("K70.30", MLD), ("K70.9", MLD), ("K71.2", None),
    ("K71.3", MLD), ("K71.50", MLD), ("K71.6", None), ("K71.7", MLD), ("K73.9", MLD), ("K74.60", MLD),
    ("K76.0", MLD), ("K76.1", None), ("K76.2", MLD), ("K76.4", MLD), ("K76.89", MLD), ("Z94.4", MLD),
    ("Z94.5", None),
    # Moderate or severe liver: I85.0, I85.9, I86.4, I98.2, K70.4, K71.1, K72.1, K72.9, K76.5-K76.7.
    ("I85.00", SLD), ("I85.10", None), ("I85.91", SLD), ("I86.4", SLD), ("I98.2", SLD), ("K70.40", SLD),
    ("K71.11", SLD), ("K72.10", SLD), ("K72.00", None), ("K72.91", SLD), ("K76.5", SLD), ("K76.7", SLD),
    # Diabetes: .0 .1 .6 .8 .9 without; .2 .3 .4 .5 .7 with chronic complication; E10-E14 only (not E08/E09).
    ("E10.9", DM), ("E11.65", DM), ("E11.8", DM), ("E13.10", DM), ("E11.21", DMC), ("E11.3553", DMC),
    ("E10.40", DMC), ("E11.51", DMC), ("E08.22", None), ("E09.9", None), ("E15", None),
    # Any malignancy: C00-C26, C30-C34, C37-C41, C43, C45-C58, C60-C76, C81-C85, C88, C90-C97.
    ("C00.0", TUMOR), ("C26.9", TUMOR), ("C30.0", TUMOR), ("C34.90", TUMOR), ("C37", TUMOR), ("C41.9", TUMOR),
    ("C43.9", TUMOR), ("C44.91", None), ("C45.0", TUMOR), ("C58", TUMOR), ("C60.9", TUMOR), ("C76.0", TUMOR),
    ("C7A.010", None), ("C81.90", TUMOR), ("C85.90", TUMOR), ("C86.0", None), ("C88.4", TUMOR), ("C90.00", TUMOR),
    ("C97", TUMOR), ("D03.9", None),
    # Metastatic solid tumour: C77-C80, and not "any tumour" as well.
    ("C77.0", METS), ("C78.7", METS), ("C80.1", METS),
    # AIDS/HIV: B20-B22, B24; not B23 or asymptomatic HIV (Z21).
    ("B20", "aids"), ("B22", "aids"), ("B23.0", None), ("B24", "aids"), ("Z21", None),
    # Quan categories LACE has no key for (peptic ulcer, hemiplegia, renal) are not proposed.
    ("K25.9", None), ("G81.90", None), ("N18.6", None),
])
def test_icd10_ranges(code, key):
    expected = [key] if key else []
    assert cc.coding_keys(ICD10CM, code) == expected
    assert cc.coding_keys(ICD10, code.replace(".", "")) == expected  # WHO ICD-10, written without the dot


def test_every_lace_key_has_codes_and_no_code_maps_twice():
    assert set(cc.ICD10_PREFIXES) == lace.COMORBIDITIES.keys()
    prefixes = [p for ps in cc.ICD10_PREFIXES.values() for p in ps]
    assert len(prefixes) == len(set(prefixes))
    assert not [(a, b) for a in prefixes for b in prefixes if a != b and a.startswith(b)]


def test_other_systems_and_bad_codes_propose_nothing():
    assert cc.coding_keys("http://loinc.org", "I50.9") == []
    assert cc.coding_keys(None, "I50.9") == []
    assert cc.coding_keys(ICD10CM, "") == [] and cc.coding_keys(ICD10CM, "I5") == []
    assert cc.coding_keys(ICD10CM, " i50.9 ") == [CHF]


def test_snomed_exact_concepts_only():
    assert cc.coding_keys(SCT, "84114007") == [CHF]
    assert cc.coding_keys(SCT, "128462008") == [METS]
    assert cc.coding_keys(SCT, "38341003") == []  # hypertension: not a Charlson condition
    assert cc.coding_keys(SCT, "I50.9") == []


def test_propose_lists_sources_in_worksheet_order_and_both_grades():
    out = cc.propose([
        {"name": "Cirrhosis", "codings": [{"system": ICD10CM, "code": "K74.60"}]},
        {"name": "Oesophageal varices", "codings": [{"system": ICD10CM, "code": "I85.00"},
                                                     {"system": SCT, "code": "28670008"}]},
        {"name": "Old MI", "codings": [{"system": ICD10CM, "code": "I25.2"}, {"system": ICD10CM, "code": "I25.2"}]},
        {"name": "Hypertension", "codings": [{"system": ICD10CM, "code": "I10"}]},
        {"name": "Uncoded", "codings": []},
    ])
    assert [p["key"] for p in out] == [MI, MLD, SLD]
    assert out[0] == {"key": MI, "label": "Previous myocardial infarction", "weight": 1,
                      "sources": [{"condition": "Old MI", "system": "ICD-10-CM", "code": "I25.2"}]}
    assert [s["system"] for s in out[2]["sources"]] == ["ICD-10-CM", "SNOMED CT"]
    # Proposing never scores: only what a clinician records counts, and the worse liver grade supersedes the mild.
    assert lace.charlson_index([p["key"] for p in out]) == 1 + 4

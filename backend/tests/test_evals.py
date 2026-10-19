"""The golden-set eval gate: passes on the grounded mock, and fails on hallucinations or PHI leaks."""
import json

import pytest

from app.ai import evals
from app.ai.deid import Deidentifier
from app.ai.llm import LLMResponse, MockLLMClient

pytestmark = pytest.mark.anyio


class HallucinatingClient(MockLLMClient):
    """The mock, but it adds a medication and a diagnosis that no input mentions."""

    async def complete(self, **kwargs) -> LLMResponse:
        resp = await super().complete(**kwargs)
        doc = json.loads(resp.text)
        if "medications" not in doc:  # a patient-instructions draft: see InstructionsHallucinatingClient
            return resp
        doc["medications"].append({"name": "warfarin", "dose": "5 mg", "frequency": "daily", "instructions": ""})
        doc["discharge_diagnoses"].append("Atrial fibrillation")
        return self._reply(json.dumps(doc), kwargs["messages"])


class InstructionsHallucinatingClient(MockLLMClient):
    """The mock, but patient instructions name a medicine the signed summary doesn't, and drop the others."""

    async def complete(self, **kwargs) -> LLMResponse:
        resp = await super().complete(**kwargs)
        doc = json.loads(resp.text)
        if "medicines" not in doc:
            return resp
        doc["medicines"] = [{"name": "warfarin", "how_to_take": "Take 5 mg daily.", "purpose": ""}]
        return self._reply(json.dumps(doc), kwargs["messages"])


class HardToReadClient(MockLLMClient):
    """The mock, but English patient instructions in dense clinical prose."""

    async def complete(self, **kwargs) -> LLMResponse:
        resp = await super().complete(**kwargs)
        doc = json.loads(resp.text)
        if "medicines" in doc:
            doc["what_happened"] = ("Pharmacological management necessitated intravenous antimicrobial administration "
                                    "notwithstanding considerable gastrointestinal intolerance and electrolyte "
                                    "derangement attributable to concomitant medications.")
        return self._reply(json.dumps(doc), kwargs["messages"])


async def test_golden_set_passes_on_mock():
    report = await evals.run_evals(MockLLMClient())
    assert report["ok"], json.dumps(report["summary"])
    assert report["summary"]["cases"] >= 8 and report["summary"]["mean_coverage"] == 1.0
    instructions = report["patient_instructions"]
    assert instructions["ok"] and instructions["summary"]["cases"] >= 4
    assert {r["language"] for r in instructions["results"]} >= {"en", "es"}
    assert all(r["reading_grade"] <= 6 for r in instructions["results"] if r["language"] == "en")


async def test_instruction_evals_fail_on_invented_or_missing_medicines():
    report = await evals.run_evals(InstructionsHallucinatingClient())
    assert not report["ok"] and report["checks"]["hallucinations"] is True  # summaries are untouched
    section = report["patient_instructions"]
    assert section["checks"]["hallucinations"] is False
    kinds = {(h["type"], h["value"]) for r in section["results"] for h in r["hallucinations"]}
    assert ("medication", "warfarin") in kinds and ("omission", "amoxicillin") in kinds


async def test_instruction_evals_fail_above_the_target_grade():
    report = await evals.run_evals(HardToReadClient())
    section = report["patient_instructions"]
    assert not report["ok"] and section["checks"]["readability"] is False
    assert section["summary"]["above_target_grade"] == 3  # the English cases; Spanish is not measured


async def test_eval_fails_on_hallucinated_meds_and_diagnoses():
    report = await evals.run_evals(HallucinatingClient())
    assert not report["ok"] and report["checks"]["hallucinations"] is False
    flagged = {h["value"] for r in report["results"] for h in r["hallucinations"]}
    assert flagged == {"warfarin", "Atrial fibrillation"}


async def test_eval_fails_when_phi_reaches_the_model(monkeypatch):
    monkeypatch.setattr(Deidentifier, "add_person", lambda self, kind, name, *a, **k: name)
    report = await evals.run_evals(MockLLMClient())
    assert not report["ok"] and report["checks"]["phi_leaks"] is False

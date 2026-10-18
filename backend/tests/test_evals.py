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
        doc["medications"].append({"name": "warfarin", "dose": "5 mg", "frequency": "daily", "instructions": ""})
        doc["discharge_diagnoses"].append("Atrial fibrillation")
        return self._reply(json.dumps(doc), kwargs["messages"])


async def test_golden_set_passes_on_mock():
    report = await evals.run_evals(MockLLMClient())
    assert report["ok"], json.dumps(report["summary"])
    assert report["summary"]["cases"] >= 8 and report["summary"]["mean_coverage"] == 1.0


async def test_eval_fails_on_hallucinated_meds_and_diagnoses():
    report = await evals.run_evals(HallucinatingClient())
    assert not report["ok"] and report["checks"]["hallucinations"] is False
    flagged = {h["value"] for r in report["results"] for h in r["hallucinations"]}
    assert flagged == {"warfarin", "Atrial fibrillation"}


async def test_eval_fails_when_phi_reaches_the_model(monkeypatch):
    monkeypatch.setattr(Deidentifier, "add_person", lambda self, kind, name, *a, **k: name)
    report = await evals.run_evals(MockLLMClient())
    assert not report["ok"] and report["checks"]["phi_leaks"] is False

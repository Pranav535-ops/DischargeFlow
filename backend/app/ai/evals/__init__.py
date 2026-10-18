"""Golden-set evaluation of discharge-summary generation.

Runs every synthetic case in golden_set.json through exactly the production path (ai.service.summary_facts and
run_summary_model: de-identification, the active prompt version, structured output, retry, grounding) and scores:

- coverage: share of expected facts present in the summary (the mean must meet the threshold),
- hallucinations: medications or diagnoses in the output that are not in the input (must be zero),
- forbidden facts: text that must never appear (e.g. a drug that an injected instruction asked for),
- PHI leaks: any direct identifier of the synthetic patient found in what was sent to the model.

`python -m app.ai.evals` exits non-zero when any threshold fails, which is how CI gates prompt and model changes.
"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

from app.ai.deid import Deidentifier
from app.ai.llm import LLMClient, LLMResponse
from app.ai.prompts import get_prompt
from app.ai.service import run_summary_model, summary_facts
from app.patients.models import Patient

GOLDEN_SET = Path(__file__).resolve().parent / "golden_set.json"


class _Recorder:
    """Wraps a client and keeps everything sent to it, for the PHI-leak check."""

    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.provider = inner.provider
        self.model_id = inner.model_id
        self.sent: list[str] = []

    async def complete(self, **kwargs) -> LLMResponse:
        self.sent.append(json.dumps({k: kwargs[k] for k in ("system", "messages")}))
        return await self.inner.complete(**kwargs)


@dataclass
class CaseResult:
    id: str
    status: str
    coverage: float
    missing_facts: list[str] = field(default_factory=list)
    hallucinations: list[dict] = field(default_factory=list)
    forbidden_found: list[str] = field(default_factory=list)
    phi_leaks: list[str] = field(default_factory=list)
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @property
    def passed(self) -> bool:
        return (self.status == "succeeded" and not self.hallucinations and not self.forbidden_found
                and not self.phi_leaks)


async def run_case(case: dict, client: LLMClient) -> CaseResult:
    p = case["patient"]
    patient = Patient(name=p["name"], date_of_birth=p["date_of_birth"], admission_date=p["admission_date"],
                      room_number=p.get("room_number", "4B"), diagnosis=p["diagnosis"],
                      attending_physician=p["attending_physician"])
    tasks = [SimpleNamespace(title=t, status="completed") for t in case.get("completed_tasks", [])]
    recorder = _Recorder(client)
    deid = Deidentifier()
    facts = summary_facts(patient, tasks, case.get("clinical_notes", ""), deid)
    prov, result = await run_summary_model(recorder, facts, deid)

    sent = "\n".join(recorder.sent).lower()
    identifiers = [p["name"], *p["name"].split(), p["date_of_birth"], p["admission_date"], p["attending_physician"]]
    leaks = sorted({i for i in identifiers if len(i) > 2 and i.lower() in sent})

    text = (result.content or "").lower()
    expected = case.get("expected_facts", [])
    missing = [f for f in expected if f.lower() not in text]
    coverage = 1.0 if not expected else round((len(expected) - len(missing)) / len(expected), 3)
    return CaseResult(
        id=case["id"], status=prov["status"], coverage=coverage if result.content else 0.0, missing_facts=missing,
        hallucinations=prov["grounding_warnings"],
        forbidden_found=[f for f in case.get("forbidden_facts", []) if f.lower() in text],
        phi_leaks=leaks, attempts=prov["attempts"], input_tokens=prov["input_tokens"],
        output_tokens=prov["output_tokens"], latency_ms=prov["latency_ms"],
    )


async def run_evals(client: LLMClient, golden_path: Path = GOLDEN_SET) -> dict:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    results = [await run_case(case, client) for case in golden["cases"]]
    t = golden["thresholds"]
    summary = {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "mean_coverage": round(sum(r.coverage for r in results) / len(results), 3) if results else 0.0,
        "hallucinations": sum(len(r.hallucinations) + len(r.forbidden_found) for r in results),
        "phi_leaks": sum(len(r.phi_leaks) for r in results),
        "failures": sum(r.status != "succeeded" for r in results),
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
    }
    checks = {
        "coverage": summary["mean_coverage"] >= t["min_mean_coverage"],
        "hallucinations": summary["hallucinations"] <= t["max_hallucinations"],
        "phi_leaks": summary["phi_leaks"] <= t["max_phi_leaks"],
        "failures": summary["failures"] <= t["max_failures"],
    }
    return {"provider": client.provider, "model_id": client.model_id,
            "prompt_version": get_prompt("discharge_summary").id, "thresholds": t, "summary": summary,
            "checks": checks, "ok": all(checks.values()),
            "results": [{**asdict(r), "passed": r.passed} for r in results]}

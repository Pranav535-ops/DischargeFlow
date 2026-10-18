"""Admin AI insights: aggregates only (no PHI), citations checked against the input, provenance and audit."""
import dataclasses
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.ai import llm
from app.ai.llm import LLMResponse, MockLLMClient
from app.analytics import router as analytics_router
from app.core.db import utcnow
from app.discharge.models import Task

pytestmark = pytest.mark.anyio


async def _activity(client, db, make_user, create_patient, patients=3):
    """Discharges with completed tasks whose durations differ by role (pharmacist slowest)."""
    _, physician = await make_user("physician")
    names = []
    for i in range(patients):
        p = await create_patient(physician, name=f"Insight Patient{i}")
        names.append(p["name"])
        await client.post(f"/api/workflows/initiate/{p['patient_id']}", headers=physician)
    now = utcnow()
    hours = {"pharmacist": 20, "nurse": 4, "physician": 2}
    for t in await db.scalars(select(Task)):
        if t.category in ("medication", "education", "documentation"):
            t.status = "completed"
            t.created_at = now - timedelta(hours=hours[t.assigned_role])
            t.completed_at = now
    await db.commit()
    return names


async def test_aggregates_are_admin_only_and_contain_no_phi(client, db, make_user, create_patient):
    names = await _activity(client, db, make_user, create_patient)
    _, admin = await make_user("admin")
    _, physician = await make_user("physician")
    assert (await client.get("/api/analytics/aggregates", headers=physician)).status_code == 403

    agg = (await client.get("/api/analytics/aggregates", headers=admin)).json()
    raw = json.dumps(agg)
    assert not any(n in raw for n in names) and "pat_" not in raw and "wf_" not in raw
    assert agg["task_completion_time_by_role"]["pharmacist"]["avg_hours"] == pytest.approx(20, abs=0.1)
    assert agg["open_tasks_by_role"]["nurse"]["n"] > 0


async def test_small_groups_hide_averages(client, db, make_user, create_patient):
    await _activity(client, db, make_user, create_patient, patients=1)
    _, admin = await make_user("admin")
    agg = (await client.get("/api/analytics/aggregates", headers=admin)).json()
    pharmacist = agg["task_completion_time_by_role"]["pharmacist"]
    assert pharmacist["n"] == 2 and "avg_hours" not in pharmacist  # fewer than 3: count only


async def test_insights_cite_real_metrics_and_record_provenance(client, db, make_user, create_patient):
    await _activity(client, db, make_user, create_patient)
    _, admin = await make_user("admin")
    body = (await client.post("/api/analytics/insights", headers=admin)).json()

    findings = body["insights"]["findings"]
    assert findings[0]["metric"] == "task_completion_time_by_role.pharmacist.avg_hours"
    assert "Pharmacist" in body["insights"]["headline"] and body["rejected_citations"] == []
    assert body["generation"]["prompt_version"] == "insights@v1" and body["generation"]["status"] == "succeeded"

    logs = (await client.get("/api/audit/logs?resource_type=analytics", headers=admin)).json()
    assert [e["action"] for e in logs] == ["AI_INSIGHTS_GENERATED"]


async def test_findings_citing_unknown_metrics_are_dropped(client, db, make_user, create_patient, monkeypatch):
    class Inventive(MockLLMClient):
        async def complete(self, **kwargs) -> LLMResponse:
            doc = {"headline": "h", "findings": [
                {"title": "real", "detail": "d", "metric": "discharge_duration.n", "suggestion": "s"},
                {"title": "made up", "detail": "Readmissions rose 40%", "metric": "readmission_rate", "suggestion": "s"},
            ]}
            return self._reply(json.dumps(doc), kwargs["messages"])

    monkeypatch.setattr(analytics_router, "get_llm_client", lambda purpose: Inventive())
    await _activity(client, db, make_user, create_patient)
    _, admin = await make_user("admin")
    body = (await client.post("/api/analytics/insights", headers=admin)).json()
    assert [f["title"] for f in body["insights"]["findings"]] == ["real"]
    assert body["rejected_citations"] == ["readmission_rate"]


async def test_metrics_still_shown_when_ai_is_disabled(client, db, make_user, create_patient, monkeypatch):
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, llm_provider="none"))
    await _activity(client, db, make_user, create_patient)
    _, admin = await make_user("admin")
    body = (await client.post("/api/analytics/insights", headers=admin)).json()
    assert body["insights"] is None and body["unavailable"] and body["aggregates"]["discharge_duration"] == {"n": 0}

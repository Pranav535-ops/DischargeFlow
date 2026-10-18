"""Governed AI summaries: de-identification, prompt registry, structured output + retry, provenance, approval diff."""
import json

import pytest
from sqlalchemy import select

from app.ai import service as ai_service
from app.ai.deid import Deidentifier, age_from_dob
from app.ai.grounding import check_grounding
from app.ai.llm import LLMError, LLMResponse, MockLLMClient, mock_summary
from app.ai.models import AIGeneration, DischargeSummary
from app.ai.prompts import ACTIVE, available_versions, get_prompt
from app.ai.schemas import SUMMARY_JSON_SCHEMA, DischargeSummaryDoc

pytestmark = pytest.mark.anyio

NOTES = (
    "Course: Jane Doe was treated with IV ceftriaxone, switched to oral amoxicillin on day 3. Afebrile for 48 hours.\n"
    "Medications: amoxicillin 500 mg three times daily for 5 days; acetaminophen 500 mg every 6 hours as needed\n"
    "Follow-up: Primary care in 7 days\n"
    "Call 555-201-3344 or jane.doe@example.com with questions. Seen 2026-10-02."
)

# Pinned hashes of every published prompt version. Editing a published version in place fails the test below:
# change a prompt by adding the next version. Add an entry here when publishing a version, never to "fix" an edit.
PROMPT_HASHES = {
    "discharge_summary@v1": "15acf765ac42c0570e67ad7e24c7e0ff1c5d14786c38756b03148a187acf88f8",
    "assistant@v1": "4140a6aa7316d38e6a579cef7c9b3602a2b589dc7c196a3a2d5ddc018bd77b44",
    "insights@v1": "55564b79e7f99df666f619a16f94d717ba7ba9d7e1dea3eb89b2d30421bef9c9",
}


class RecordingClient(MockLLMClient):
    """The mock, plus a record of every request and optional scripted replies (text) to return first."""

    def __init__(self, replies: list[str] | None = None, fail: bool = False):
        super().__init__()
        self.calls: list[dict] = []
        self.replies = list(replies or [])
        self.fail = fail

    async def complete(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        if self.fail:
            raise LLMError("provider returned 529")
        if self.replies:
            return self._reply(self.replies.pop(0), kwargs["messages"])
        return await super().complete(**kwargs)


@pytest.fixture
def recording(monkeypatch):
    def install(**kwargs) -> RecordingClient:
        client = RecordingClient(**kwargs)
        monkeypatch.setattr(ai_service, "get_llm_client", lambda purpose: client)
        return client

    return install


async def _workflow(client, make_user, create_patient):
    _, physician = await make_user("physician")
    patient = await create_patient(physician)
    resp = await client.post(f"/api/workflows/initiate/{patient['patient_id']}", headers=physician)
    return physician, patient, resp.json()["workflow"]["workflow_id"]


async def _generate(client, headers, patient, wf_id, notes=NOTES):
    return await client.post("/api/ai/generate-summary", headers=headers,
                              json={"patient_id": patient["patient_id"], "workflow_id": wf_id, "clinical_notes": notes})


# ---------- de-identification ----------

def test_deidentifier_round_trip_and_scrubbing():
    d = Deidentifier()
    tok = d.add_person("PATIENT", "Jane Doe", identity="pat_1")
    doc = d.add_person("PHYSICIAN", "Dr. Test", match_parts=False)
    text = d.redact("Jane Doe (Doe, Jane) seen by Dr. Test. Phone 555-201-3344, MRN 99812, on 10/02/2026. Blood test ok.")
    assert "Jane" not in text and "Doe" not in text and "555" not in text and "99812" not in text
    assert "10/02" not in text and "Blood test ok" in text  # "test" the word is not the physician's name
    assert d.reidentify(f"{tok} discharged by {doc}") == "Jane Doe discharged by Dr. Test"


def test_shared_names_are_redacted_not_leaked():
    d = Deidentifier()
    first = d.add_person("PATIENT", "John Smith", identity="pat_1")
    second = d.add_person("PATIENT", "John Smith", identity="pat_2")
    assert first != second
    assert "Smith" not in d.redact("Give John Smith his meds") and "[AMBIGUOUS_NAME]" in d.redact("John Smith")


def test_age_is_capped_for_safe_harbor():
    assert age_from_dob("1920-01-01") == 90
    assert age_from_dob("not a date") is None


# ---------- prompt registry and schema ----------

def test_prompt_registry_versions_and_hashes():
    for name, version in ACTIVE.items():
        prompt = get_prompt(name)
        assert prompt.id == f"{name}@{version}" and len(prompt.sha256) == 64 and prompt.system
        assert version in available_versions(name)
    assert "$facts_json" in get_prompt("discharge_summary").user


def test_published_prompts_are_immutable():
    for name in ACTIVE:
        for version in available_versions(name):
            prompt_id = f"{name}@{version}"
            assert prompt_id in PROMPT_HASHES, f"{prompt_id} is new: pin its hash in PROMPT_HASHES"
            assert get_prompt(name, version).sha256 == PROMPT_HASHES[prompt_id], f"{prompt_id} was edited in place"


def test_summary_schema_is_closed_and_inlined():
    assert "$defs" not in json.dumps(SUMMARY_JSON_SCHEMA) and "$ref" not in json.dumps(SUMMARY_JSON_SCHEMA)
    assert SUMMARY_JSON_SCHEMA["additionalProperties"] is False
    assert set(SUMMARY_JSON_SCHEMA["required"]) == set(DischargeSummaryDoc.model_fields)
    med = SUMMARY_JSON_SCHEMA["properties"]["medications"]["items"]
    assert med["additionalProperties"] is False and set(med["required"]) == {"name", "dose", "frequency", "instructions"}


def test_grounding_flags_unsupported_meds_and_diagnoses():
    facts = {"diagnosis": "Community-acquired pneumonia", "clinical_notes": NOTES}
    doc = DischargeSummaryDoc.model_validate(mock_summary(facts))
    assert check_grounding(doc, json.dumps(facts)) == []

    bad = doc.model_copy(update={
        "discharge_diagnoses": ["Community-acquired pneumonia", "Atrial fibrillation"],
        "hospital_course": doc.hospital_course + " Started warfarin.",
    })
    warnings = check_grounding(bad, json.dumps(facts))
    assert {w["value"] for w in warnings} == {"warfarin", "Atrial fibrillation"}


# ---------- generation API ----------

async def test_generate_summary_deidentifies_and_records_provenance(client, make_user, create_patient, db, recording):
    llm = recording()
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    resp = await _generate(client, physician, patient, wf_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Nothing identifying reached the model.
    sent = json.dumps(llm.calls)
    for identifier in ("Jane Doe", "1970-01-01", "12A", "Dr. Test", "555-201-3344", "jane.doe@example.com",
                       "2026-10-02", patient["patient_id"]):
        assert identifier not in sent, identifier
    assert "[PATIENT_1]" in sent and llm.calls[0]["json_schema"] == SUMMARY_JSON_SCHEMA

    # The stored draft is re-identified, structured, and unapproved.
    assert body["approved"] is False and "Jane Doe" in body["content"]
    assert [m["name"] for m in body["structured"]["medications"]] == ["amoxicillin", "acetaminophen"]
    assert body["original_content"] == body["content"]

    gen = body["generation"]
    assert gen["prompt_version"] == "discharge_summary@v1" and gen["model_id"] == "mock-1"
    assert gen["status"] == "succeeded" and gen["attempts"] == 1 and len(gen["input_hash"]) == 64
    assert gen["grounding_warnings"] == [] and gen["input_tokens"] > 0
    assert body["generation_id"] == gen["generation_id"]

    listed = (await client.get(f"/api/ai/generations?workflow_id={wf_id}", headers=physician)).json()
    assert [g["generation_id"] for g in listed] == [gen["generation_id"]]


async def test_invalid_output_is_retried_once(client, make_user, create_patient, recording):
    llm = recording(replies=['{"hospital_course": "missing everything else"}'])
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    resp = await _generate(client, physician, patient, wf_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["generation"]["attempts"] == 2
    assert "failed validation" in llm.calls[1]["messages"][0]["content"]


async def test_repeated_invalid_output_fails_visibly_and_keeps_provenance(client, make_user, create_patient, db,
                                                                         recording):
    recording(replies=["not json", '{"also": "wrong"}'])
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    resp = await _generate(client, physician, patient, wf_id)
    assert resp.status_code == 502 and resp.json()["status"] == "invalid_output"

    assert await db.scalar(select(DischargeSummary).where(DischargeSummary.workflow_id == wf_id)) is None
    gen = await db.get(AIGeneration, resp.json()["generation_id"])
    assert gen.status == "invalid_output" and gen.attempts == 2
    _, admin = await make_user("admin")
    actions = [e["action"] for e in (await client.get("/api/audit/logs?resource_type=discharge_summary",
                                                      headers=admin)).json()]
    assert "AI_SUMMARY_FAILED" in actions

    # A later attempt can still succeed.
    recording()
    assert (await _generate(client, physician, patient, wf_id)).status_code == 200


async def test_provider_error_fails_visibly(client, make_user, create_patient, recording):
    recording(fail=True)
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    resp = await _generate(client, physician, patient, wf_id)
    assert resp.status_code == 502 and resp.json()["status"] == "failed"


async def test_hallucinated_medication_is_flagged_on_the_draft(client, make_user, create_patient, recording):
    fake = mock_summary({"diagnosis": "Community-acquired pneumonia", "clinical_notes": NOTES})
    fake["medications"].append({"name": "warfarin", "dose": "5 mg", "frequency": "daily", "instructions": ""})
    recording(replies=[json.dumps(fake)])
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    body = (await _generate(client, physician, patient, wf_id)).json()
    assert [w["value"] for w in body["generation"]["grounding_warnings"]] == ["warfarin"]


async def test_approval_records_physician_edit_diff(client, make_user, create_patient, db):
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    summary = (await _generate(client, physician, patient, wf_id)).json()
    edited = summary["content"].replace("Primary care in 7 days", "Primary care in 5 days, pulmonology in 6 weeks")

    resp = await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", json={"content": edited},
                            headers=physician)
    assert resp.status_code == 200, resp.text
    assert resp.json()["edited"] is True and 0.8 < resp.json()["edit_similarity"] < 1

    row = await db.get(DischargeSummary, summary["summary_id"])
    assert row.approved and row.content == edited and row.original_content == summary["content"]
    assert "-- Primary care in 7 days" in row.edit_diff and "+- Primary care in 5 days" in row.edit_diff
    assert (await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)).status_code == 400

    metrics = (await client.get("/api/ai/metrics", headers=physician)).json()
    [row] = metrics["summary_quality"]
    assert row["prompt_version"] == "discharge_summary@v1" and row["approved"] == 1 and row["approval_rate"] == 1.0
    assert row["avg_edit_similarity"] == resp.json()["edit_similarity"]


async def test_unedited_approval_has_similarity_one(client, make_user, create_patient):
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    summary = (await _generate(client, physician, patient, wf_id)).json()
    resp = await client.put(f"/api/ai/approve-summary/{summary['summary_id']}", headers=physician)
    assert resp.json()["edited"] is False and resp.json()["edit_similarity"] == 1.0


async def test_nurse_cannot_generate_or_approve(client, make_user, create_patient):
    _, nurse = await make_user("nurse")
    physician, patient, wf_id = await _workflow(client, make_user, create_patient)
    assert (await _generate(client, nurse, patient, wf_id)).status_code == 403
    assert (await client.get("/api/ai/metrics", headers=nurse)).status_code == 403


# ---------- Claude adapter (no network: a fake SDK client returning real SDK types) ----------

class _FakeMessages:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs = None

    async def create(self, **kwargs):
        import anthropic

        self.kwargs = kwargs
        return anthropic.types.Message.model_validate(self.payload)


def _message(content, stop_reason="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 45}}


async def test_claude_adapter_sends_schema_and_parses_usage():
    from types import SimpleNamespace

    from app.ai.llm import ClaudeClient

    doc = json.dumps(mock_summary({"diagnosis": "Pneumonia"}))
    fake = _FakeMessages(_message([{"type": "text", "text": doc}]))
    client = ClaudeClient(SimpleNamespace(messages=fake), "anthropic", "claude-sonnet-5-5", "medium")
    resp = await client.complete(system="s", messages=[{"role": "user", "content": "u"}], max_tokens=100,
                                 json_schema=SUMMARY_JSON_SCHEMA)
    assert fake.kwargs["output_config"] == {"effort": "medium",
                                            "format": {"type": "json_schema", "schema": SUMMARY_JSON_SCHEMA}}
    assert "tools" not in fake.kwargs and "thinking" not in fake.kwargs and "tool_choice" not in fake.kwargs
    assert resp.text == doc and (resp.input_tokens, resp.output_tokens) == (120, 45)
    assert resp.model_id == "claude-sonnet-5-5" and resp.provider == "anthropic"


async def test_claude_adapter_tool_calls_and_refusal():
    from types import SimpleNamespace

    from app.ai.llm import ClaudeClient

    fake = _FakeMessages(_message([{"type": "text", "text": "Looking."},
                                   {"type": "tool_use", "id": "toolu_1", "name": "list_patients", "input": {}}],
                                  stop_reason="tool_use"))
    client = ClaudeClient(SimpleNamespace(messages=fake), "anthropic", "claude-haiku-5-5", "low")
    resp = await client.complete(system="s", messages=[{"role": "user", "content": "u"}], max_tokens=100,
                                 tools=[{"name": "list_patients", "description": "d", "input_schema": {"type": "object"}}])
    assert [c.name for c in resp.tool_calls] == ["list_patients"] and resp.stop_reason == "tool_use"
    assert resp.content[1]["type"] == "tool_use" and resp.content[1]["id"] == "toolu_1"

    fake.payload = _message([], stop_reason="refusal")
    with pytest.raises(LLMError):
        await client.complete(system="s", messages=[{"role": "user", "content": "u"}], max_tokens=100)

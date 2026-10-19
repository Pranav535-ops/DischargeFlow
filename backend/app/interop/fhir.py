"""Map DischargeFlow records to FHIR R4 resources (ADR-001 Phase 5, item 19).

Read-only and deliberately small: the four resources a receiving system needs to follow a discharge.

| DischargeFlow        | FHIR R4             | Notes |
|----------------------|---------------------|-------|
| Patient              | Patient             | Erased patients keep their id but expose no name or birth date. |
| Workflow (discharge) | Encounter           | The inpatient stay being discharged: admission date to discharge completion. |
| Task                 | Task                | Discharge tasks, with the responsible role as performerType. |
| DischargeSummary     | DocumentReference   | Signed summaries only (LOINC 18842-5): latest signed version + addenda. |
"""
import base64
from datetime import date, datetime
from typing import Optional

FHIR_VERSION = "4.0.1"
FHIR_JSON = "application/fhir+json"
ID_SYSTEM = "urn:dischargeflow:id"

ENCOUNTER_STATUS = {"in_progress": "in-progress", "pending_approval": "in-progress", "completed": "finished"}
TASK_STATUS = {"pending": "requested", "in_progress": "in-progress", "completed": "completed"}
TASK_PRIORITY = {1: "urgent", 2: "routine", 3: "routine"}
ROLE_DISPLAY = {"physician": "Physician", "nurse": "Nurse", "pharmacist": "Pharmacist", "admin": "Administrator"}


def _instant(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _date(value: Optional[str]) -> Optional[str]:
    """FHIR `date`: only emitted when the stored value really is an ISO date."""
    try:
        return date.fromisoformat((value or "")[:10]).isoformat()
    except ValueError:
        return None


def _drop_none(obj):
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None and v != [] and v != {}}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


def _ref(resource_type: str, rid: Optional[str]) -> Optional[dict]:
    return {"reference": f"{resource_type}/{rid}"} if rid else None


def patient(p) -> dict:
    erased = bool(p.gdpr_erased)
    return _drop_none({
        "resourceType": "Patient",
        "id": p.patient_id,
        "meta": {"lastUpdated": _instant(p.updated_at)},
        "identifier": [{"system": ID_SYSTEM, "value": p.patient_id}],
        "active": p.status != "discharged" and not erased,
        "name": None if erased else [{"text": p.name}],
        "birthDate": None if erased else _date(p.date_of_birth),
    })


def encounter(wf, p) -> dict:
    return _drop_none({
        "resourceType": "Encounter",
        "id": wf.workflow_id,
        "meta": {"lastUpdated": _instant(wf.completed_at or wf.started_at)},
        "identifier": [{"system": ID_SYSTEM, "value": wf.workflow_id}],
        "status": ENCOUNTER_STATUS.get(wf.status, "unknown"),
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP",
                  "display": "inpatient encounter"},
        "subject": _ref("Patient", wf.patient_id),
        "period": {"start": _date(p.admission_date) if p else None, "end": _instant(wf.completed_at)},
        "reasonCode": None if (not p or p.gdpr_erased) else [{"text": p.diagnosis}],
    })


def task(t) -> dict:
    return _drop_none({
        "resourceType": "Task",
        "id": t.task_id,
        "meta": {"lastUpdated": _instant(t.updated_at)},
        "identifier": [{"system": ID_SYSTEM, "value": t.task_id}],
        "status": TASK_STATUS.get(t.status, "requested"),
        "intent": "order",
        "priority": TASK_PRIORITY.get(t.priority, "routine"),
        "code": {"text": t.category},
        "description": t.title,
        "note": [{"text": t.description}] if t.description else None,
        "for": _ref("Patient", t.patient_id),
        "encounter": _ref("Encounter", t.workflow_id),
        "authoredOn": _instant(t.created_at),
        "lastModified": _instant(t.updated_at),
        "performerType": [{"text": ROLE_DISPLAY.get(t.assigned_role, t.assigned_role)}],
        "executionPeriod": {"end": _instant(t.completed_at)} if t.completed_at else None,
    })


def _text_attachment(text: str, title: str, created: Optional[datetime]) -> dict:
    return {"attachment": {
        "contentType": "text/plain; charset=utf-8",
        "language": "en",
        "title": title,
        "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "creation": _instant(created),
    }}


def document_reference(s) -> dict:
    """A signed discharge summary: the latest signed version (including any physician edits made before signing),
    then each addendum to it as a further attachment, oldest first. With addenda the document is `amended`."""
    version = s.latest_version
    addenda = s.addenda_for(version)
    # A summary signed before migration 0013 has no version row until `app.cli migrate` backfills it.
    text, signed_at = (version.content, version.signed_at) if version else (s.content, s.approved_at)
    content = [_text_attachment(text, "Discharge summary" + (f" (version {version.version})" if version else ""),
                                signed_at)]
    content += [_text_attachment(a.content, f"Addendum {i} by {a.signed_by}", a.signed_at)
                for i, a in enumerate(addenda, start=1)]
    return _drop_none({
        "resourceType": "DocumentReference",
        "id": s.summary_id,
        "meta": {"lastUpdated": _instant(addenda[-1].signed_at if addenda else signed_at)},
        "identifier": [{"system": ID_SYSTEM, "value": s.summary_id}],
        "status": "current",
        "docStatus": "amended" if addenda else "final",
        "type": {"coding": [{"system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary"}]},
        "subject": _ref("Patient", s.patient_id),
        "date": _instant(signed_at),
        "authenticator": {"display": f"Approving physician ({s.approved_by})"} if s.approved_by else None,
        "description": "AI-drafted, physician-reviewed and approved discharge summary" if s.generation_id
        else "Discharge summary",
        "content": content,
        "context": {"encounter": [_ref("Encounter", s.workflow_id)]},
    })


def bundle(resources: list[dict], base_url: str) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"fullUrl": f"{base_url}{r['resourceType']}/{r['id']}", "resource": r, "search": {"mode": "match"}}
                  for r in resources],
    }


def operation_outcome(code: str, diagnostics: str) -> dict:
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}]}


SEARCH_PARAMS = {
    "Patient": [("_id", "token")],
    "Encounter": [("_id", "token"), ("patient", "reference")],
    "Task": [("_id", "token"), ("patient", "reference"), ("encounter", "reference")],
    "DocumentReference": [("_id", "token"), ("patient", "reference"), ("encounter", "reference")],
}


def capability_statement(base_url: str, software_version: str) -> dict:
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": date.today().isoformat(),
        "kind": "instance",
        "software": {"name": "DischargeFlow", "version": software_version},
        "implementation": {"description": "DischargeFlow FHIR R4 read-only API", "url": base_url},
        "fhirVersion": FHIR_VERSION,
        "format": ["json"],
        "rest": [{
            "mode": "server",
            "security": {"description": "OAuth bearer access token from DischargeFlow sign-in; every read is audited."},
            "resource": [{
                "type": rtype,
                "interaction": [{"code": "read"}, {"code": "search-type"}],
                "searchParam": [{"name": n, "type": t} for n, t in params] + [{"name": "_count", "type": "number"}],
            } for rtype, params in SEARCH_PARAMS.items()],
        }],
    }

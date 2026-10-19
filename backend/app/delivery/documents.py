"""The documents a signed discharge summary is delivered as (ADR-002 item 16).

- `fhir_bundle`: a FHIR R4 document Bundle (`type: document`). The first entry is a Composition (LOINC 18842-5,
  Discharge summary) with sections, then the Patient, Encounter, the signing Practitioner and the custodian
  Organization. Entries use `urn:uuid:` full URLs and every reference resolves inside the Bundle.
- `ccda`: an HL7 C-CDA R2.1 Discharge Summary (US Realm Header + Discharge Summary templates), as XML.

The signed text is the authoritative content: the physician may have edited the AI draft before signing, so the
structured draft is never sent. Sections without structured data in DischargeFlow (allergies, discharge medications)
are present as C-CDA requires, marked "no information". Neither format has been run through an official validator
(the HL7 FHIR validator, the ONC C-CDA validator); tests check the structure and that the XML is well formed.

Both are deterministic for the same input (ids are UUIDv5 of DischargeFlow ids), so a retried delivery sends the
same document with the same identifier.
"""
import json
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

LOINC = "http://loinc.org"
LOINC_OID = "2.16.840.1.113883.6.1"
DISCHARGE_SUMMARY_CODE = "18842-5"
ID_SYSTEM = "urn:dischargeflow:id"
MRN_SYSTEM = "urn:dischargeflow:mrn"
VISIT_SYSTEM = "urn:dischargeflow:visit-number"
# A fixed namespace for UUIDv5 ids derived from DischargeFlow ids (stable across retries and releases).
_NAMESPACE = uuid.UUID("6f1c3c1e-2d3e-5b8a-9a51-6b0b8d3c1f16")
CDA_NS = "urn:hl7-org:v3"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

CONTENT_TYPES = {"fhir_bundle": "application/fhir+json", "ccda": "application/xml"}


@dataclass
class Addendum:
    addendum_id: str
    text: str
    signed_by_name: str
    signed_at: datetime


@dataclass
class SummaryDocument:
    """Everything a delivered document contains, assembled by the runner from the signed summary."""

    delivery_id: str
    summary_id: str
    version_id: str
    version: int
    signed_text: str
    signed_at: datetime
    signer_id: str
    signer_name: str
    patient_id: str
    patient_name: str
    birth_date: Optional[str]
    mrn: Optional[str]
    mrn_authority: Optional[str]
    diagnosis: Optional[str]
    encounter_id: Optional[str]
    visit_number: Optional[str]
    admitted_at: Optional[datetime]
    discharged_at: Optional[datetime]
    disposition: Optional[str]
    tenant_id: str
    organization_name: str
    recipient_name: str
    addenda: list[Addendum] = field(default_factory=list)
    created_at: Optional[datetime] = None

    @property
    def amended(self) -> bool:
        return bool(self.addenda)

    @property
    def document_id(self) -> str:
        """The same document (the same signature and addenda) to the same receiver keeps one id across retries."""
        return _uuid("document", self.delivery_id)

    @property
    def timestamp(self) -> datetime:
        return self.addenda[-1].signed_at if self.addenda else self.signed_at


def _uuid(*parts: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, ":".join(parts)))


def _urn(*parts: str) -> str:
    return f"urn:uuid:{_uuid(*parts)}"


def _iso_date(value: Optional[str]) -> Optional[str]:
    try:
        return date.fromisoformat((value or "")[:10]).isoformat()
    except ValueError:
        return None


def _drop_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None and v != [] and v != {}}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


# ---------------------------------------------------------------- FHIR R4 document Bundle

def _xhtml(paragraphs: list[str]) -> str:
    div = ET.Element("div", {"xmlns": "http://www.w3.org/1999/xhtml"})
    for text in paragraphs:
        ET.SubElement(div, "p").text = text
    return ET.tostring(div, encoding="unicode")


def _paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p] or [""]


def _section(title: str, paragraphs: list[str], code: Optional[tuple[str, str]] = None) -> dict:
    return _drop_none({
        "title": title,
        "code": {"coding": [{"system": LOINC, "code": code[0], "display": code[1]}]} if code else None,
        "text": {"status": "generated", "div": _xhtml(paragraphs)},
    })


def fhir_bundle(doc: SummaryDocument) -> dict:
    patient_url = _urn("Patient", doc.tenant_id, doc.patient_id)
    encounter_url = _urn("Encounter", doc.tenant_id, doc.encounter_id) if doc.encounter_id else None
    author_url = _urn("Practitioner", doc.tenant_id, doc.signer_id)
    org_url = _urn("Organization", doc.tenant_id)
    composition_url = _urn("Composition", doc.document_id)

    identifiers: list[dict[str, Any]] = [{"system": ID_SYSTEM, "value": doc.patient_id}]
    if doc.mrn:
        identifiers.append({
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]},
            "system": f"{MRN_SYSTEM}:{doc.mrn_authority}" if doc.mrn_authority else MRN_SYSTEM,
            "value": doc.mrn,
        })
    patient = _drop_none({
        "resourceType": "Patient", "id": _uuid("Patient", doc.tenant_id, doc.patient_id),
        "identifier": identifiers, "name": [{"text": doc.patient_name}],
        "birthDate": _iso_date(doc.birth_date),
    })
    encounter = _drop_none({
        "resourceType": "Encounter", "id": _uuid("Encounter", doc.tenant_id, doc.encounter_id or ""),
        "identifier": [{"system": ID_SYSTEM, "value": doc.encounter_id}]
        + ([{"system": VISIT_SYSTEM, "value": doc.visit_number}] if doc.visit_number else []),
        "status": "finished" if doc.discharged_at else "in-progress",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP",
                  "display": "inpatient encounter"},
        "subject": {"reference": patient_url},
        "period": {"start": doc.admitted_at.isoformat() if doc.admitted_at else None,
                   "end": doc.discharged_at.isoformat() if doc.discharged_at else None},
        "hospitalization": {"dischargeDisposition": {"coding": [
            {"system": "http://terminology.hl7.org/CodeSystem/v2-0112", "code": doc.disposition}]}}
        if doc.disposition else None,
        "serviceProvider": {"reference": org_url},
    }) if encounter_url else None
    practitioner = {"resourceType": "Practitioner", "id": _uuid("Practitioner", doc.tenant_id, doc.signer_id),
                    "identifier": [{"system": ID_SYSTEM, "value": doc.signer_id}],
                    "name": [{"text": doc.signer_name}]}
    organization = {"resourceType": "Organization", "id": _uuid("Organization", doc.tenant_id),
                    "identifier": [{"system": ID_SYSTEM, "value": doc.tenant_id}], "name": doc.organization_name}

    sections = [_section(f"Discharge summary (signed version {doc.version})", _paragraphs(doc.signed_text),
                         (DISCHARGE_SUMMARY_CODE, "Discharge summary"))]
    if doc.diagnosis:
        sections.append(_section("Discharge diagnosis", [doc.diagnosis], ("11535-2", "Hospital discharge Dx")))
    for i, a in enumerate(doc.addenda, start=1):
        sections.append(_section(f"Addendum {i} ({a.signed_by_name}, {a.signed_at.isoformat()})",
                                 _paragraphs(a.text)))
    composition = _drop_none({
        "resourceType": "Composition", "id": _uuid("Composition", doc.document_id),
        "identifier": {"system": ID_SYSTEM, "value": doc.summary_id},
        "status": "amended" if doc.amended else "final",
        "type": {"coding": [{"system": LOINC, "code": DISCHARGE_SUMMARY_CODE, "display": "Discharge summary"}]},
        "subject": {"reference": patient_url},
        "encounter": {"reference": encounter_url} if encounter_url else None,
        "date": doc.timestamp.isoformat(),
        "author": [{"reference": author_url}],
        "title": "Discharge summary",
        "confidentiality": "N",
        "attester": [{"mode": "legal", "time": doc.signed_at.isoformat(), "party": {"reference": author_url}}],
        "custodian": {"reference": org_url},
        "section": sections,
    })
    resources = [(composition_url, composition), (patient_url, patient)]
    if encounter_url and encounter:
        resources.append((encounter_url, encounter))
    resources += [(author_url, practitioner), (org_url, organization)]
    return {
        "resourceType": "Bundle",
        "id": doc.document_id,
        "meta": {"lastUpdated": doc.timestamp.isoformat()},
        "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{doc.document_id}"},
        "type": "document",
        "timestamp": doc.timestamp.isoformat(),
        "entry": [{"fullUrl": url, "resource": resource} for url, resource in resources],
    }


# ---------------------------------------------------------------- C-CDA R2.1 Discharge Summary

def _ts(value: Optional[datetime]) -> Optional[str]:
    """HL7 V3 TS: YYYYMMDDHHMMSS+ZZZZ."""
    return value.strftime("%Y%m%d%H%M%S%z") if value else None


def _el(parent: ET.Element, tag: str, attrs: Optional[dict] = None, text: Optional[str] = None) -> ET.Element:
    e = ET.SubElement(parent, f"{{{CDA_NS}}}{tag}", {k: v for k, v in (attrs or {}).items() if v is not None})
    if text is not None:
        e.text = text
    return e


def _template(parent: ET.Element, root: str, extension: Optional[str] = None) -> None:
    _el(parent, "templateId", {"root": root, "extension": extension})


def _person_name(parent: ET.Element, full: str) -> None:
    """The record keeps one name string; C-CDA wants given and family. The last word is taken as the family name."""
    name = _el(parent, "name")
    words = full.split()
    for given in words[:-1]:
        _el(name, "given", text=given)
    _el(name, "family", text=words[-1] if words else full)


def _oid_id(parent: ET.Element, tag: str, kind: str, value: str, tenant_id: str) -> None:
    # Local ids have no OID; a UUID root names the id space (tenant + kind), the DischargeFlow id is the extension.
    _el(parent, tag, {"root": _uuid("id-space", tenant_id, kind), "extension": value})


def _no_info_section(body: ET.Element, template: tuple[str, str], code: tuple[str, str], title: str,
                     text: str) -> None:
    comp = _el(body, "component")
    section = _el(comp, "section", {"nullFlavor": "NI"})
    _template(section, *template)
    _el(section, "code", {"code": code[0], "codeSystem": LOINC_OID, "codeSystemName": "LOINC",
                          "displayName": code[1]})
    _el(section, "title", text=title)
    _el(section, "text", text=text)


def _text_section(body: ET.Element, template: Optional[tuple[str, Optional[str]]], code: Optional[tuple[str, str]],
                  title: str, paragraphs: list[str]) -> None:
    comp = _el(body, "component")
    section = _el(comp, "section")
    if template:
        _template(section, *template)
    if code:
        _el(section, "code", {"code": code[0], "codeSystem": LOINC_OID, "codeSystemName": "LOINC",
                              "displayName": code[1]})
    _el(section, "title", text=title)
    text = _el(section, "text")
    for p in paragraphs:
        _el(text, "paragraph", text=p)


def _assigned_person(parent: ET.Element, tag: str, doc: SummaryDocument, time: datetime,
                     signature: bool = False) -> None:
    role = _el(parent, tag)
    _el(role, "time", {"value": _ts(time)})
    if signature:
        _el(role, "signatureCode", {"code": "S"})
    assigned = _el(role, "assignedEntity" if tag == "legalAuthenticator" else "assignedAuthor")
    _oid_id(assigned, "id", "user", doc.signer_id, doc.tenant_id)
    person = _el(assigned, "assignedPerson")
    _person_name(person, doc.signer_name)


def ccda(doc: SummaryDocument) -> bytes:
    ET.register_namespace("", CDA_NS)
    ET.register_namespace("xsi", XSI_NS)
    root = ET.Element(f"{{{CDA_NS}}}ClinicalDocument")
    _el(root, "realmCode", {"code": "US"})
    _el(root, "typeId", {"root": "2.16.840.1.113883.1.3", "extension": "POCD_HD000040"})
    _template(root, "2.16.840.1.113883.10.20.22.1.1", "2015-08-01")  # US Realm Header
    _template(root, "2.16.840.1.113883.10.20.22.1.8", "2015-08-01")  # Discharge Summary
    _el(root, "id", {"root": doc.document_id})
    _el(root, "code", {"code": DISCHARGE_SUMMARY_CODE, "codeSystem": LOINC_OID, "codeSystemName": "LOINC",
                       "displayName": "Discharge summary"})
    _el(root, "title", text="Discharge summary")
    _el(root, "effectiveTime", {"value": _ts(doc.timestamp)})
    _el(root, "confidentialityCode", {"code": "N", "codeSystem": "2.16.840.1.113883.5.25"})
    _el(root, "languageCode", {"code": "en-US"})
    # One set per summary; the version number counts the signature plus its addenda.
    _el(root, "setId", {"root": _uuid("set", doc.summary_id)})
    _el(root, "versionNumber", {"value": str(1 + len(doc.addenda))})

    patient_role = _el(_el(root, "recordTarget"), "patientRole")
    if doc.mrn:
        _el(patient_role, "id", {"root": _uuid("mrn", doc.tenant_id, doc.mrn_authority or ""), "extension": doc.mrn,
                                 "assigningAuthorityName": doc.mrn_authority})
    _oid_id(patient_role, "id", "patient", doc.patient_id, doc.tenant_id)
    _el(patient_role, "addr", {"nullFlavor": "UNK"})
    _el(patient_role, "telecom", {"nullFlavor": "UNK"})
    person = _el(patient_role, "patient")
    _person_name(person, doc.patient_name)
    _el(person, "administrativeGenderCode", {"nullFlavor": "UNK"})
    birth = _iso_date(doc.birth_date)
    _el(person, "birthTime", {"value": birth.replace("-", "")} if birth else {"nullFlavor": "UNK"})

    _assigned_person(root, "author", doc, doc.signed_at)
    custodian = _el(_el(_el(root, "custodian"), "assignedCustodian"), "representedCustodianOrganization")
    _oid_id(custodian, "id", "organization", doc.tenant_id, doc.tenant_id)
    _el(custodian, "name", text=doc.organization_name)
    _el(custodian, "telecom", {"nullFlavor": "UNK"})
    _el(custodian, "addr", {"nullFlavor": "UNK"})
    recipient = _el(_el(_el(root, "informationRecipient"), "intendedRecipient"), "receivedOrganization")
    _el(recipient, "name", text=doc.recipient_name)
    _assigned_person(root, "legalAuthenticator", doc, doc.signed_at, signature=True)

    encounter = _el(_el(root, "componentOf"), "encompassingEncounter")
    _oid_id(encounter, "id", "encounter", doc.encounter_id or doc.summary_id, doc.tenant_id)
    period = _el(encounter, "effectiveTime")
    _el(period, "low", {"value": _ts(doc.admitted_at)} if doc.admitted_at else {"nullFlavor": "UNK"})
    _el(period, "high", {"value": _ts(doc.discharged_at)} if doc.discharged_at else {"nullFlavor": "UNK"})
    if doc.disposition:
        _el(encounter, "dischargeDispositionCode", {"code": doc.disposition, "codeSystem": "2.16.840.1.113883.12.112"})

    body = _el(_el(root, "component"), "structuredBody")
    _no_info_section(body, ("2.16.840.1.113883.10.20.22.2.6", "2015-08-01"), ("48765-2", "Allergies and adverse reactions"),
                     "Allergies", "No structured allergy information is recorded in DischargeFlow.")
    _text_section(body, ("1.3.6.1.4.1.19376.1.5.3.1.3.5", None), ("8648-8", "Hospital course"),
                  f"Hospital course (signed discharge summary, version {doc.version})", _paragraphs(doc.signed_text))
    if doc.diagnosis:
        _text_section(body, ("2.16.840.1.113883.10.20.22.2.24", "2015-08-01"), ("11535-2", "Hospital discharge Dx"),
                      "Discharge diagnosis", [doc.diagnosis])
    else:
        _no_info_section(body, ("2.16.840.1.113883.10.20.22.2.24", "2015-08-01"),
                         ("11535-2", "Hospital discharge Dx"), "Discharge diagnosis", "No information.")
    _no_info_section(body, ("2.16.840.1.113883.10.20.22.2.11", "2015-08-01"),
                     ("10183-2", "Hospital discharge medications"), "Discharge medications",
                     "No structured medication list is recorded in DischargeFlow; see the hospital course.")
    _text_section(body, ("2.16.840.1.113883.10.20.22.2.10", "2014-06-09"), ("18776-5", "Plan of care note"),
                  "Plan of treatment", ["Follow-up and instructions are in the signed discharge summary above."])
    for i, a in enumerate(doc.addenda, start=1):
        _text_section(body, None, None, f"Addendum {i} ({a.signed_by_name}, {a.signed_at.isoformat()})",
                      _paragraphs(a.text))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def render(doc: SummaryDocument, document_format: str) -> bytes:
    if document_format == "ccda":
        return ccda(doc)
    return json.dumps(fhir_bundle(doc), separators=(",", ":"), ensure_ascii=False).encode("utf-8")

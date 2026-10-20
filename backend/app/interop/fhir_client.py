"""Outbound FHIR R4 client for the EHR (ADR-002 items 14 and 15).

Used with the EHR access token of a SMART launch, to read the launch user's Practitioner, the launch patient's
identifiers, and the clinical context: active MedicationRequest, Condition and AllergyIntolerance. Read-only.

Every request has short timeouts, goes only to the launch's FHIR base (`iss`; pagination links elsewhere are not
followed, so the token never leaves for another host), and nothing fetched is stored: clinical context is kept in a
small per-process cache for CACHE_SECONDS and otherwise refetched.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx

from app.risk import charlson_codes

TIMEOUT = httpx.Timeout(8.0, connect=4.0)
MAX_PAGES = 5
PAGE_SIZE = 100
CACHE_SECONDS = 60.0
CACHE_MAX_ENTRIES = 256
FHIR_JSON = "application/fhir+json"


class FhirError(Exception):
    """A FHIR request failed. `status` is the HTTP status (None: network/timeout/invalid JSON)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def http_client() -> httpx.AsyncClient:
    """Every outbound request to an EHR goes through a client made here (tests swap it for a fake server)."""
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


def same_origin(url: str, base: str) -> bool:
    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower()) and a.path.startswith(b.path.rstrip("/"))


class FhirClient:
    def __init__(self, base: str, access_token: str):
        self.base = base.rstrip("/")
        self._token = access_token

    def url(self, reference: str) -> str:
        """An absolute URL for `Patient/123` or an absolute reference, which must be on this server."""
        url = reference if reference.startswith(("http://", "https://")) else f"{self.base}/{reference.lstrip('/')}"
        if not same_origin(url, self.base):
            raise FhirError("reference is not on the launch's FHIR server")
        return url

    async def _get(self, http: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> dict:
        try:
            resp = await http.get(url, params=params,
                                  headers={"Authorization": f"Bearer {self._token}", "Accept": FHIR_JSON})
        except httpx.HTTPError as e:
            raise FhirError(type(e).__name__)
        if resp.status_code != 200:
            raise FhirError(f"HTTP {resp.status_code}", resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            raise FhirError("invalid JSON")
        if not isinstance(body, dict):
            raise FhirError("invalid FHIR resource")
        return body

    async def read(self, reference: str) -> dict:
        async with http_client() as http:
            return await self._get(http, self.url(reference))

    async def search(self, resource_type: str, params: dict) -> list[dict]:
        """Resources of `resource_type` from a search, following `next` links on this server (MAX_PAGES)."""
        out: list[dict] = []
        async with http_client() as http:
            url: Optional[str] = f"{self.base}/{resource_type}"
            query: Optional[dict] = {**params, "_count": str(PAGE_SIZE)}
            for _ in range(MAX_PAGES):
                assert url is not None
                bundle = await self._get(http, url, query)
                for entry in bundle.get("entry") or []:
                    res = entry.get("resource") if isinstance(entry, dict) else None
                    if isinstance(res, dict) and res.get("resourceType") == resource_type:
                        out.append(res)
                url = next((link.get("url") for link in bundle.get("link") or []
                            if isinstance(link, dict) and link.get("relation") == "next"), None)
                if not url or not same_origin(url, self.base):
                    break
                query = None  # the next link carries the query
        return out


# ---------- parsing (only what the screens and the summary need) ----------

def _text(concept: Any) -> str:
    if not isinstance(concept, dict):
        return ""
    if concept.get("text"):
        return str(concept["text"]).strip()
    for coding in concept.get("coding") or []:
        if isinstance(coding, dict) and (coding.get("display") or coding.get("code")):
            return str(coding.get("display") or coding.get("code")).strip()
    return ""


def _code(concept: Any) -> str:
    if isinstance(concept, dict):
        for coding in concept.get("coding") or []:
            if isinstance(coding, dict) and coding.get("code"):
                return str(coding["code"])
    return ""


def _codings(concept: Any) -> list[dict]:
    """(system, code) pairs of a CodeableConcept, for the Charlson proposals; never shown or sent on their own."""
    if not isinstance(concept, dict):
        return []
    return [{"system": str(c.get("system") or ""), "code": str(c["code"])[:20]}
            for c in concept.get("coding") or [] if isinstance(c, dict) and c.get("code")][:10]


EXCLUDED_VERIFICATION = {"entered-in-error", "refuted"}
INACTIVE_CLINICAL = {"inactive", "resolved", "remission"}


def _excluded(resource: dict) -> bool:
    return (_code(resource.get("verificationStatus")) in EXCLUDED_VERIFICATION
            or _code(resource.get("clinicalStatus")) in INACTIVE_CLINICAL)


def medication(resource: dict) -> Optional[dict]:
    if resource.get("status") in ("entered-in-error", "cancelled", "completed", "stopped"):
        return None
    name = _text(resource.get("medicationCodeableConcept"))
    if not name and isinstance(resource.get("medicationReference"), dict):
        name = str(resource["medicationReference"].get("display") or "").strip()
    if not name:
        return None
    dosage = next((str(d.get("text")).strip() for d in resource.get("dosageInstruction") or []
                   if isinstance(d, dict) and d.get("text")), "")
    return {"name": name[:200], "dosage": dosage[:300], "status": str(resource.get("status") or "")[:30]}


def problem(resource: dict) -> Optional[dict]:
    if _excluded(resource):
        return None
    name = _text(resource.get("code"))
    if not name:
        return None
    category = next((_code(c) for c in resource.get("category") or [] if _code(c)), "")
    return {"name": name[:200], "clinical_status": _code(resource.get("clinicalStatus"))[:30],
            "category": category[:40]}


def allergy(resource: dict) -> Optional[dict]:
    if _excluded(resource):
        return None
    substance = _text(resource.get("code"))
    if not substance:
        return None
    reactions = sorted({_text(m) for r in resource.get("reaction") or [] if isinstance(r, dict)
                        for m in r.get("manifestation") or []} - {""})
    return {"substance": substance[:200], "criticality": str(resource.get("criticality") or "")[:20],
            "reactions": [r[:100] for r in reactions[:5]]}


SEARCHES: tuple[tuple[str, str, dict[str, str], Callable[[dict], Optional[dict]]], ...] = (
    ("medications", "MedicationRequest", {"status": "active"}, medication),
    ("problems", "Condition", {"clinical-status": "active"}, problem),
    ("allergies", "AllergyIntolerance", {}, allergy),
)


@dataclass
class ClinicalContext:
    medications: list[dict] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    allergies: list[dict] = field(default_factory=list)
    # Resource types that could not be read, e.g. {"allergies": "HTTP 403"}. The others are still returned.
    errors: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0
    unauthorized: bool = False  # the EHR refused the token (expired or revoked there)
    # LACE Charlson flags proposed from the problem list's ICD-10 / SNOMED CT codes (app.risk.charlson_codes), for
    # the plan screen to offer a clinician. Not in as_dict(): the summary path (smart.summary_context) never sees it.
    charlson_proposals: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"medications": self.medications, "problems": self.problems, "allergies": self.allergies,
                "errors": self.errors}

    def counts(self) -> str:
        return f"{len(self.medications)} medications, {len(self.problems)} problems, {len(self.allergies)} allergies"


async def fetch_clinical_context(client: FhirClient, ehr_patient: str) -> ClinicalContext:
    ctx = ClinicalContext(fetched_at=time.time())
    for key, resource_type, params, parse in SEARCHES:
        try:
            resources = await client.search(resource_type, {"patient": ehr_patient, **params})
        except FhirError as e:
            ctx.errors[key] = str(e)
            ctx.unauthorized = ctx.unauthorized or e.status == 401
            continue
        parsed = [(item, r) for item, r in ((parse(r), r) for r in resources) if item]
        # Dedupe repeated entries (same name) the EHR may return for reorders.
        seen: set[str] = set()
        for item, _ in parsed:
            name = (item.get("name") or item.get("substance") or "").lower()
            if name not in seen:
                seen.add(name)
                getattr(ctx, key).append(item)
        if key == "problems":
            ctx.charlson_proposals = charlson_codes.propose(
                {"name": item["name"], "codings": _codings(r.get("code"))} for item, r in parsed)
    return ctx


# ---------- per-process cache (no persistent copy) ----------

_cache: dict[str, tuple[float, ClinicalContext]] = {}


def cached(launch_id: str) -> Optional[ClinicalContext]:
    hit = _cache.get(launch_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    _cache.pop(launch_id, None)
    return None


def remember(launch_id: str, ctx: ClinicalContext) -> None:
    if ctx.errors:
        return  # a partial result is not kept: the next read tries again
    if len(_cache) >= CACHE_MAX_ENTRIES:
        now = time.monotonic()
        for key in [k for k, (exp, _) in _cache.items() if exp <= now] or list(_cache)[: CACHE_MAX_ENTRIES // 4]:
            _cache.pop(key, None)
    _cache[launch_id] = (time.monotonic() + CACHE_SECONDS, ctx)


def forget(launch_id: Optional[str] = None) -> None:
    if launch_id is None:
        _cache.clear()
    else:
        _cache.pop(launch_id, None)

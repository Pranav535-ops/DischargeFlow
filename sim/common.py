"""Shared pieces of the hospital simulation: the hospital's shape, synthetic people, HL7 builders and an API client.

Everything here is synthetic. The simulation talks to the API only (dev login, the integration client's API key),
never to the database, so it runs against any deployment where `ENABLE_DEV_LOGIN` is on (APP_ENV=test or
development). See sim/README.md.
"""
from __future__ import annotations

import json
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".state.json"  # gitignored: the target deployment, unit ids, seeded staff (no credentials)
OUT_DIR = HERE / "out"
HL7_MEDIA_TYPE = "x-application/hl7-v2+er7"

# 24 units, named the way ADT systems usually send the point of care (PV1-3.1).
UNIT_CODES = [f"{floor}{wing}" for floor in range(3, 9) for wing in ("W", "E", "N", "S")]
SENDING_FACILITY = "SIMHOSP"
ASSIGNING_AUTHORITY = "SIMHOSP"

# Where patients go (ADR-002 item 8), weighted roughly like a US acute hospital.
DISPOSITION_WEIGHTS = {"home": 60, "home_services": 15, "snf": 12, "rehab": 5, "hospice": 3, "ama": 2, "transfer": 3}
# HL7 table 0007 admission type: E emergency, U urgent, R routine (elective).
ADMISSION_TYPES = [("E", 55), ("U", 25), ("R", 20)]

STAFF = {"nurse": 120, "physician": 40, "pharmacist": 12, "admin": 3}

DIAGNOSES = [
    ("J18.9", "Pneumonia, unspecified organism"), ("I50.9", "Heart failure, unspecified"),
    ("J44.1", "Chronic obstructive pulmonary disease with acute exacerbation"), ("N39.0", "Urinary tract infection"),
    ("A41.9", "Sepsis, unspecified organism"), ("I63.9", "Cerebral infarction, unspecified"),
    ("K35.80", "Acute appendicitis"), ("S72.001A", "Fracture of neck of right femur"),
    ("E11.65", "Type 2 diabetes mellitus with hyperglycemia"), ("I21.4", "Non-ST elevation myocardial infarction"),
    ("K85.90", "Acute pancreatitis"), ("L03.115", "Cellulitis of right lower limb"),
]
GIVEN = ["Ada", "Ben", "Chloe", "Dev", "Elena", "Farid", "Grace", "Hiro", "Isla", "Jonah", "Kavya", "Liam", "Maya",
         "Noah", "Olu", "Priya", "Quinn", "Ravi", "Sofia", "Tariq", "Uma", "Victor", "Wen", "Ximena", "Yusuf", "Zara"]
FAMILY = ["Abbott", "Banerjee", "Castillo", "Dimitrov", "Eze", "Fischer", "Garcia", "Haddad", "Ivanova", "Jensen",
          "Kowalski", "Lindqvist", "Mensah", "Nakamura", "Okafor", "Petrov", "Quispe", "Rossi", "Silva", "Tanaka",
          "Umarov", "Varga", "Walsh", "Xu", "Yilmaz", "Zielinski"]
ATTENDINGS = [("Sim", "Avery"), ("Sim", "Blake"), ("Sim", "Casey"), ("Sim", "Drew"), ("Sim", "Emery")]


def weighted(rng: random.Random, weights: dict | list) -> str:
    items = list(weights.items()) if isinstance(weights, dict) else weights
    return rng.choices([k for k, _ in items], weights=[w for _, w in items])[0]


def hl7_ts(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------- synthetic patients ----------

@dataclass
class SimPatient:
    mrn: str
    given: str
    family: str
    dob: str  # YYYYMMDD
    sex: str
    unit: str
    room: str
    bed: str
    visit: str
    admitted_at: datetime
    admission_type: str
    diagnosis: tuple[str, str]
    attending: tuple[str, str] = field(default=("Sim", "Avery"))


def make_patient(rng: random.Random, mrn: str, visit: str, unit: str, room: str, bed: str,
                 admitted_at: datetime) -> SimPatient:
    age_days = rng.randint(18 * 365, 95 * 365)
    dob = (datetime.now(timezone.utc) - timedelta(days=age_days)).strftime("%Y%m%d")
    return SimPatient(mrn=mrn, given=rng.choice(GIVEN), family=rng.choice(FAMILY), dob=dob, sex=rng.choice("FM"),
                      unit=unit, room=room, bed=bed, visit=visit, admitted_at=admitted_at,
                      admission_type=weighted(rng, ADMISSION_TYPES), diagnosis=rng.choice(DIAGNOSES),
                      attending=rng.choice(ATTENDINGS))


# ---------- HL7 v2 builders (ER7) ----------

def _msh(event: str, control_id: str, when: datetime, structure: str = "ADT_A01") -> str:
    return (f"MSH|^~\\&|SIMEHR|{SENDING_FACILITY}|DISCHARGEFLOW|{SENDING_FACILITY}|{hl7_ts(when)}||"
            f"ADT^{event}^{structure}|{control_id}|P|2.5.1")


def _pid(p: SimPatient) -> str:
    return f"PID|1||{p.mrn}^^^{ASSIGNING_AUTHORITY}^MR||{p.family}^{p.given}||{p.dob}|{p.sex}"


def _pv1(p: SimPatient, *, discharged_at: datetime | None = None, disposition: str = "") -> str:
    pv1 = ["PV1", "1", "I", f"{p.unit}^{p.room}^{p.bed}", p.admission_type] + [""] * 41
    pv1[7] = f"^{p.attending[1]}^{p.attending[0]}^^^Dr."
    pv1[19] = p.visit
    pv1[36] = disposition
    pv1[44] = hl7_ts(p.admitted_at)
    pv1[45] = hl7_ts(discharged_at) if discharged_at else ""
    return "|".join(pv1)


def adt_message(event: str, control_id: str, p: SimPatient, when: datetime, *, prior_mrn: str | None = None,
                disposition: str = "") -> str:
    segments = [_msh(event, control_id, when, "ADT_A39" if event == "A40" else "ADT_A01"),
                f"EVN|{event}|{hl7_ts(when)}", _pid(p)]
    if event == "A40":
        segments.append(f"MRG|{prior_mrn}^^^{ASSIGNING_AUTHORITY}^MR")
    segments.append(_pv1(p, discharged_at=when if event == "A03" else None, disposition=disposition))
    if event in ("A01", "A08"):
        segments.append(f"DG1|1||{p.diagnosis[0]}^{p.diagnosis[1]}^I10")
    return "\r".join(segments) + "\r"


def ack_code(body: str) -> tuple[str, str]:
    """(MSA-1 code, MSA-3 text) from an ACK."""
    for line in body.split("\r"):
        if line.startswith("MSA|"):
            parts = line.split("|")
            return parts[1], parts[3] if len(parts) > 3 else ""
    return "??", body[:120]


# ---------- API client ----------

class Api:
    """A signed-in API session (dev login). Re-signs in once on 401, since access tokens last 15 minutes."""

    def __init__(self, base_url: str, role: str, handle: str = "", timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.role, self.handle = role, handle
        self.http = httpx.Client(base_url=f"{self.base}/api", timeout=timeout)
        self.user: dict = {}
        self.login()

    def login(self) -> None:
        resp = self.http.post("/auth/dev-login", json={"role": self.role, "handle": self.handle})
        if resp.status_code == 404:
            raise SystemExit(f"{self.base}: dev login is off. The sim needs APP_ENV=test and ENABLE_DEV_LOGIN=true.")
        resp.raise_for_status()
        body = resp.json()
        self.user = body["user"]
        self.http.headers["Authorization"] = f"Bearer {body['token']}"

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = self.http.request(method, path, **kwargs)
        if resp.status_code == 401:
            self.login()
            resp = self.http.request(method, path, **kwargs)
        return resp

    def get(self, path: str, **kw) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> httpx.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> httpx.Response:
        return self.request("PUT", path, **kw)


@contextmanager
def integration_key(admin: Api, purpose: str) -> Iterator[str]:
    """A short-lived ADT integration client: created for one run and revoked when it ends, so its key is never
    written anywhere (it lives only in this process's memory)."""
    resp = admin.post("/integrations/clients", json={"name": f"Sim {purpose} {datetime.now(timezone.utc):%Y-%m-%d %H:%M}"})
    resp.raise_for_status()
    body = resp.json()
    try:
        yield body["api_key"]
    finally:
        admin.request("DELETE", f"/integrations/clients/{body['client_id']}")


def post_hl7(http: httpx.Client, api_key: str, message: str) -> tuple[str, str, float]:
    """Send one ER7 message; returns (ack code, ack text, seconds)."""
    started = time.perf_counter()
    resp = http.post("/hl7/adt", content=message.encode(),
                     headers={"X-API-Key": api_key, "Content-Type": HL7_MEDIA_TYPE})
    elapsed = time.perf_counter() - started
    if resp.status_code != 200:
        return f"HTTP{resp.status_code}", resp.text[:200], elapsed
    code, text = ack_code(resp.text)
    return code, text, elapsed

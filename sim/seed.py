"""Seed a synthetic hospital into a sim deployment: units, staff with unit memberships, an ADT integration client,
admitted patients (through HL7 A01, as a real hospital would load them) and a discharge plan for each stay.

    python sim/seed.py --base-url https://<sim deployment> [--occupancy 0.9] [--beds-per-unit 18]

Idempotent: units and users are matched by code and handle, and a patient whose MRN is already admitted is skipped
(AE 205); run it again to top the census back up. The ADT feed uses a short-lived integration client, revoked when the
seed ends, so no key is stored. sim/.state.json keeps the deployment, unit ids and staff list only.
"""
from __future__ import annotations

import argparse
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx

from common import (DISPOSITION_WEIGHTS, STAFF, UNIT_CODES, Api, adt_message, integration_key, load_state,
                    make_patient, post_hl7, save_state, weighted)


def ensure_units(admin: Api) -> dict[str, str]:
    existing = {u["code"]: u["unit_id"] for u in admin.get("/units").raise_for_status().json()}
    for code in UNIT_CODES:
        if code not in existing:
            resp = admin.post("/units", json={"code": code, "name": f"Sim ward {code}"})
            resp.raise_for_status()
            existing[code] = resp.json()["unit_id"]
    return {code: existing[code] for code in UNIT_CODES}


def ensure_staff(base_url: str, admin: Api, units: dict[str, str]) -> list[dict]:
    """Sign each staff member in once (creates the user) and give them their units."""
    codes = list(units)
    staff = []
    for role, count in STAFF.items():
        for i in range(1, count + 1):
            handle = f"sim-{role[:4]}-{i:03d}"
            if role == "nurse":
                mine = [codes[(i - 1) % len(codes)]]                      # 5 nurses per unit
            elif role == "physician":
                start = ((i - 1) * 3) % len(codes)                         # each covers 3 units
                mine = [codes[(start + k) % len(codes)] for k in range(3)]
            elif role == "pharmacist":
                mine = codes[(i - 1) * 2 % len(codes):][:4] or codes[:4]   # 4 units each
            else:
                mine = []                                                   # admins see the whole tenant
            staff.append({"role": role, "handle": handle, "units": mine})

    def sign_in(member: dict) -> dict:
        api = Api(base_url, member["role"], member["handle"])
        member["user_id"] = api.user["user_id"]
        if member["units"]:
            admin.put(f"/users/{member['user_id']}/units",
                      json={"unit_ids": [units[c] for c in member["units"]]}).raise_for_status()
        return member

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(sign_in, staff))


def admit_patients(base_url: str, api_key: str, units: dict[str, str], beds_per_unit: int, occupancy: float,
                   rng: random.Random) -> dict:
    now = datetime.now(timezone.utc)
    messages = []
    for u_index, code in enumerate(units):
        for bed_no in range(1, beds_per_unit + 1):
            if rng.random() > occupancy:
                continue
            mrn = f"SIM{u_index:02d}{bed_no:03d}"
            room = f"{code[0]}{bed_no:02d}"
            p = make_patient(rng, mrn, f"V{mrn}A", code, room, "A",
                             now - timedelta(hours=rng.randint(4, 6 * 24)))
            messages.append(adt_message("A01", f"SEED-{mrn}", p, p.admitted_at))

    counts: dict[str, int] = {}

    def send(msg: str) -> str:
        with httpx.Client(base_url=f"{base_url.rstrip('/')}/api", timeout=60) as http:
            code, text, _ = post_hl7(http, api_key, msg)
        # AE 205 on a re-run: the MRN is in house, or this stay's visit number was admitted before (and may have been
        # discharged by an ADT day since). Anything else unexpected is reported.
        seen = code == "AE" and "already" in text.lower() and "admitted" in text.lower()
        return "already_admitted" if seen else code

    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(send, messages):
            counts[result] = counts.get(result, 0) + 1
    return counts


def plan_stays(admin: Api, rng: random.Random) -> int:
    """Expected discharge date (today to +5 days) and disposition for every admitted stay without one."""
    census = admin.get("/census").raise_for_status().json()
    today = date.today()
    planned = 0
    for row in census["patients"]:
        enc = row.get("encounter_id")
        if not enc or not (row.get("mrn") or "").startswith("SIM"):
            continue
        body = {"expected_discharge_date": (today + timedelta(days=rng.choice([0, 0, 1, 1, 2, 3, 4, 5]))).isoformat(),
                "planned_disposition": weighted(rng, DISPOSITION_WEIGHTS)}
        resp = admin.put(f"/encounters/{enc}/plan", json=body)
        if resp.status_code == 400 and "delay" in resp.text.lower():
            resp = admin.put(f"/encounters/{enc}/plan", json={**body, "delay_code": "medical"})
        if resp.is_success:
            planned += 1
    return planned


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--beds-per-unit", type=int, default=18)
    parser.add_argument("--occupancy", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    state = load_state()
    if state.get("base_url") not in (None, args.base_url):
        sys.exit(f"sim/.state.json belongs to {state['base_url']}; move it aside to seed another deployment.")
    state["base_url"] = args.base_url

    admin = Api(args.base_url, "admin", "sim-admi-001")
    units = ensure_units(admin)
    print(f"units: {len(units)}")
    staff = ensure_staff(args.base_url, admin, units)
    print(f"staff: {len(staff)} ({', '.join(f'{r} {n}' for r, n in STAFF.items())})")
    state.pop("api_key", None)  # written by earlier versions of this script; keys are no longer stored
    with integration_key(admin, "seed") as api_key:
        admitted = admit_patients(args.base_url, api_key, units, args.beds_per_unit, args.occupancy, rng)
    print(f"A01 admissions: {admitted}")
    print(f"plans set: {plan_stays(admin, rng)}")

    state.update({"units": units, "staff": staff, "seeded_at": datetime.now(timezone.utc).isoformat()})
    save_state(state)
    bad = {k: v for k, v in admitted.items() if k not in ("AA", "already_admitted")}
    if bad:
        sys.exit(f"unexpected ACKs while admitting: {bad}")


if __name__ == "__main__":
    main()

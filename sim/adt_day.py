"""Replay a simulated hospital day of HL7 v2 ADT traffic against a seeded sim deployment, then check the outcome.

    python sim/adt_day.py --base-url https://<sim deployment> [--duration 600] [--senders 3] [--unordered]

The day (generated from the current census, reproducible with --seed):
- ~100 A03 discharges spread 10:00–19:00, some answered by an A13 cancel shortly after
- ~100 A01 admissions of new MRNs (10% later cancelled with A11), ~40 A02 transfers, ~30 A08 updates
- out-of-order: an A03 sent before its patient's A01 (must be parked, then applied when the A01 arrives)
- duplicates: ~3% of messages resent with the same control id (must replay the original ACK, not apply twice)
- merges: a duplicate record admitted, discharged, then merged into the real patient with A40

24 simulated hours are compressed into --duration seconds. Messages for one patient keep their order (one sender per
MRN, as an interface engine does) unless --unordered, which spreads them over senders at random (test plan B2).

Checks afterwards: every ACK is the expected one, duplicates got their original ACK, nothing is left parked, the
census matches the state the ADT feed implies, and the audit chain verifies. Exits non-zero on any failure and
writes a JSON report to sim/out/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import httpx

from common import (OUT_DIR, UNIT_CODES, Api, SimPatient, adt_message, integration_key, load_state, make_patient,
                    post_hl7)


@dataclass
class Event:
    at: float            # seconds into the simulated day
    event: str
    control_id: str
    message: str
    mrn: str
    expect: tuple[str, ...] = ("AA",)  # acceptable ACK codes
    duplicate_of: str | None = None


def patient_from_row(row: dict, rng: random.Random) -> SimPatient:
    given, _, family = (row.get("name") or "Sim Patient").rpartition(" ")  # stored as "given [middle] family"
    unit, room = row.get("unit_code") or UNIT_CODES[0], row.get("room_number") or "100"
    room, _, bed = room.partition("-")
    p = make_patient(rng, row["mrn"], row.get("visit_number") or "", unit, room, bed or "A",
                     datetime.now(timezone.utc) - timedelta(days=2))
    dob = (row.get("date_of_birth") or "1950-01-01").replace("-", "")
    return replace(p, given=given or family, family=family or given, dob=dob)


def build_day(census: list[dict], rng: random.Random, day_start: datetime, tag: str) -> tuple[list[Event], dict]:
    """The events, and the expected end state: MRN -> 'in_house' | 'discharged'."""
    events: list[Event] = []
    expected: dict[str, str] = {}
    seq = 0

    def at_clock(hour_from: float, hour_to: float) -> float:
        return rng.uniform(hour_from, hour_to) * 3600

    def add(at: float, event: str, p: SimPatient, expect=("AA",), **kw) -> Event:
        nonlocal seq
        seq += 1
        control_id = f"{tag}-{seq:05d}"
        when = day_start + timedelta(seconds=at)
        ev = Event(at, event, control_id, adt_message(event, control_id, p, when, **kw), p.mrn, expect)
        events.append(ev)
        return ev

    people = [patient_from_row(r, rng) for r in census if (r.get("mrn") or "").startswith("SIM")]
    rng.shuffle(people)
    for p in people:
        expected[p.mrn] = "in_house"

    discharging, people = people[:100], people[100:]
    transferring, updating = people[:40], people[40:70]

    for p in discharging:
        t = at_clock(10, 19)
        add(t, "A03", p, disposition="01")
        expected[p.mrn] = "discharged"
        if rng.random() < 0.05:                                    # cancelled discharge: back in house
            add(t + 1800, "A13", p)
            expected[p.mrn] = "in_house"

    for p in transferring:
        unit = rng.choice(UNIT_CODES)
        add(at_clock(0, 22), "A02", replace(p, unit=unit, room=f"{unit[0]}{rng.randint(20, 40):02d}", bed="B"))
    for p in updating:
        add(at_clock(0, 22), "A08", replace(p, family=p.family + "-Lee"))

    for n in range(100):                                           # new admissions through the day
        mrn = f"SIMD{tag[-4:]}{n:03d}"
        unit = rng.choice(UNIT_CODES)
        p = make_patient(rng, mrn, f"V{mrn}", unit, f"{unit[0]}{50 + n % 40:02d}", "A",
                         day_start + timedelta(seconds=at_clock(0, 20)))
        t = (p.admitted_at - day_start).total_seconds()
        add(t, "A01", p)
        expected[mrn] = "in_house"
        if n % 10 == 0:                                            # admitted in error: A11 an hour later
            add(t + 3600, "A11", p)
            expected[mrn] = "cancelled"

    for n in range(3):                                             # A03 arrives before its A01 (parked)
        mrn = f"SIMO{tag[-4:]}{n:02d}"
        unit = rng.choice(UNIT_CODES)
        p = make_patient(rng, mrn, f"V{mrn}", unit, f"{unit[0]}90", "A", day_start + timedelta(hours=8))
        add(at_clock(9, 10), "A03", p, disposition="01")
        add(at_clock(11, 12), "A01", p)                            # the late A01 replays the parked A03
        expected[mrn] = "discharged"

    for n, survivor in enumerate(transferring[-2:]):               # a duplicate record, later merged
        dup = replace(survivor, mrn=f"SIMM{tag[-4:]}{n:02d}", visit=f"VSIMM{tag[-4:]}{n:02d}")
        add(at_clock(6, 7), "A01", dup)
        add(at_clock(7.5, 8), "A03", dup, disposition="01")
        add(at_clock(9, 9.5), "A40", survivor, prior_mrn=dup.mrn)
        expected[dup.mrn] = "merged"

    events.sort(key=lambda e: e.at)
    for original in rng.sample(events, k=max(1, len(events) * 3 // 100)):   # resends after a timeout
        events.append(replace(original, at=original.at + 5, duplicate_of=original.control_id))
    events.sort(key=lambda e: e.at)
    return events, expected


def replay(events: list[Event], base_url: str, api_key: str, duration: float, senders: int, unordered: bool,
           rng: random.Random) -> list[dict]:
    lanes: dict[int, list[Event]] = defaultdict(list)
    for ev in events:
        lane = rng.randrange(senders) if unordered else int(hashlib.sha256(ev.mrn.encode()).hexdigest(), 16) % senders
        lanes[lane].append(ev)
    scale = duration / 86400
    started = time.monotonic()
    results: list[dict] = []
    lock = threading.Lock()

    def run(lane_events: list[Event]) -> None:
        with httpx.Client(base_url=f"{base_url.rstrip('/')}/api", timeout=60) as http:
            for ev in lane_events:
                delay = started + ev.at * scale - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                code, text, secs = post_hl7(http, api_key, ev.message)
                with lock:
                    results.append({"control_id": ev.control_id, "event": ev.event, "mrn": ev.mrn, "ack": code,
                                    "text": text, "seconds": round(secs, 3), "expect": list(ev.expect),
                                    "duplicate_of": ev.duplicate_of})

    threads = [threading.Thread(target=run, args=(lane,)) for lane in lanes.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def check(results: list[dict], expected: dict, admin: Api) -> tuple[list[str], dict]:
    failures: list[str] = []
    first_ack = {r["control_id"]: r for r in results if not r["duplicate_of"]}
    for r in results:
        if r["duplicate_of"]:
            orig = first_ack.get(r["duplicate_of"])
            if orig and (orig["ack"], orig["text"]) != (r["ack"], r["text"]):
                failures.append(f"duplicate {r['duplicate_of']} got {r['ack']} '{r['text']}', first {orig['ack']}")
        elif r["ack"] not in r["expect"]:
            failures.append(f"{r['control_id']} {r['event']} {r['mrn']}: {r['ack']} {r['text']}")

    parked = admin.get("/hl7/parked", params={"status": "parked"}).json()
    if parked:
        failures.append(f"{len(parked)} message(s) still parked")

    in_house = {r["mrn"] for r in admin.get("/census").raise_for_status().json()["patients"] if r.get("mrn")}
    for mrn, state in expected.items():
        if state == "in_house" and mrn not in in_house:
            failures.append(f"{mrn} should be in house but is not on the census")
        if state in ("discharged", "merged", "cancelled") and mrn in in_house:
            failures.append(f"{mrn} should be {state} but is still on the census")

    audit = admin.get("/audit/verify").json()
    if not audit.get("valid"):
        failures.append(f"audit chain broken: {audit}")

    latencies = sorted(r["seconds"] for r in results)
    pct = (lambda q: latencies[min(len(latencies) - 1, int(q * len(latencies)))]) if latencies else (lambda q: None)
    stats = {"messages": len(results), "acks": dict(Counter(r["ack"] for r in results)),
             "latency_p50_s": pct(0.5), "latency_p95_s": pct(0.95), "latency_max_s": latencies[-1] if latencies else None,
             "audit_entries": audit.get("entries"), "census_in_house": len(in_house)}
    return failures, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--duration", type=float, default=600, help="real seconds for the 24 simulated hours")
    parser.add_argument("--senders", type=int, default=3)
    parser.add_argument("--unordered", action="store_true", help="no per-patient ordering across senders")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true", help="only write the generated messages")
    args = parser.parse_args()

    state = load_state()
    if not state.get("staff") or state.get("base_url") != args.base_url:
        sys.exit("Run sim/seed.py against this deployment first.")
    rng = random.Random(args.seed)
    admin = Api(args.base_url, "admin", "sim-admi-001")
    census = admin.get("/census").raise_for_status().json()["patients"]
    tag = datetime.now(timezone.utc).strftime("%m%d%H%M")
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    events, expected = build_day(census, rng, day_start, tag)
    counts = Counter(e.event for e in events)
    print(f"{len(events)} messages: {dict(counts)}; duplicates {sum(1 for e in events if e.duplicate_of)}")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"adt-day-{tag}.hl7").write_text("\n".join(e.message.replace("\r", "\n") for e in events))
    if args.dry_run:
        return

    with integration_key(admin, "ADT day") as api_key:  # revoked when the replay ends; never stored
        results = replay(events, args.base_url, api_key, args.duration, args.senders, args.unordered, rng)
    failures, stats = check(results, expected, admin)
    report = {"tag": tag, "base_url": args.base_url, "duration_s": args.duration, "senders": args.senders,
              "unordered": args.unordered, "stats": stats, "failures": failures, "results": results}
    path = OUT_DIR / f"adt-day-{tag}.json"
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps(stats, indent=2))
    print(f"report: {path}")
    if failures:
        print(f"{len(failures)} failure(s):", *failures[:30], sep="\n  ")
        sys.exit(1)
    print("ADT day replay passed")


if __name__ == "__main__":
    main()

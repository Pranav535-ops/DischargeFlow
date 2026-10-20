"""Outcome analytics (ADR-002 item 19): length of stay against the expected date, accuracy of the expected discharge
date, delay reasons as a Pareto, discharge before noon and 30-day readmissions, overall and by unit.

Same rules as the other aggregates (analytics/service.py): counts and durations only, never a patient-level row, and
a group under MIN_GROUP_SIZE gets its count but no averages or rates. Groups are units (by code). There is no
service line (medicine, surgery) in the data model yet, so attribution is by the unit the patient left from.
Days are hospital days (HOSPITAL_TIMEZONE).
"""
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import mean, median
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.service import MIN_GROUP_SIZE, READMISSION_DAYS
from app.core.db import utcnow
from app.discharge.models import DELAY_KINDS, DelayReason
from app.discharge.templates import hospital_zone
from app.identity.models import Unit
from app.patients.models import Encounter

NOON = 12
OVERALL = "all"
UNASSIGNED = "unassigned"


def _days(values: list[float]) -> dict:
    if len(values) < MIN_GROUP_SIZE:
        return {"n": len(values)}
    return {"n": len(values), "avg_days": round(mean(values), 2), "median_days": round(median(values), 2)}


def _share(n: int, hits: int) -> dict:
    out: dict = {"n": n, "count": hits}
    if n >= MIN_GROUP_SIZE:
        out["rate"] = round(hits / n, 3)
    return out


class _Group:
    def __init__(self) -> None:
        self.los: list[float] = []
        self.vs_expected: list[int] = []  # discharge day minus final expected day; negative is early
        self.first_error: list[int] = []
        self.final_error: list[int] = []
        self.edd_changed = 0
        self.before_noon: list[bool] = []
        self.followed: list[bool] = []  # discharges with a full READMISSION_DAYS of follow-up: readmitted?
        self.follow_up_pending = 0

    def view(self) -> dict:
        n = len(self.before_noon)
        out = {
            "discharges": n,
            "length_of_stay_days": _days(self.los),
            "vs_expected_date": {
                **_days([float(d) for d in self.vs_expected]),
                "on_or_before": _share(len(self.vs_expected), sum(d <= 0 for d in self.vs_expected))},
            "expected_date_accuracy": {
                "with_expected_date": len(self.final_error),
                "with_first_expected_date": len(self.first_error),
                "first_estimate_changed": self.edd_changed,
                "first_exact": _share(len(self.first_error), sum(e == 0 for e in self.first_error)),
                "final_exact": _share(len(self.final_error), sum(e == 0 for e in self.final_error)),
                "first_abs_error": _days([float(e) for e in self.first_error]),
                "final_abs_error": _days([float(e) for e in self.final_error]),
            },
            "before_noon": _share(n, sum(self.before_noon)),
            f"readmitted_within_{READMISSION_DAYS}_days": {
                **_share(len(self.followed), sum(self.followed)), "follow_up_pending": self.follow_up_pending},
        }
        return out


def pareto(counts: Counter) -> list[dict]:
    """Codes by count, most frequent first, with their share and the cumulative share."""
    total = sum(counts.values())
    out, running = [], 0
    for code, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        running += n
        out.append({"code": code, "n": n, "share": round(n / total, 3), "cumulative_share": round(running / total, 3)})
    return out


async def outcome_metrics(db: AsyncSession, days: int = 90) -> dict:
    """Outcomes of the stays that ended in the last `days` days."""
    now = utcnow()
    since = now - timedelta(days=days)
    zone = hospital_zone()
    codes = {u.unit_id: u.code for u in await db.scalars(select(Unit))}

    def label(unit_id: Optional[str]) -> str:
        return codes.get(unit_id or "", UNASSIGNED)

    # Every non-cancelled stay of the patients concerned, to find the next admission after each discharge.
    stays: dict[str, list] = defaultdict(list)
    for patient_id, admitted in await db.execute(
            select(Encounter.patient_id, Encounter.admitted_at)
            .where(Encounter.status != "cancelled", Encounter.admitted_at >= since - timedelta(days=1))):
        if admitted is not None:
            stays[patient_id].append(admitted)

    groups: dict[str, _Group] = defaultdict(_Group)
    for (patient_id, unit_id, admitted, left, expected, first) in await db.execute(
            select(Encounter.patient_id, Encounter.unit_id, Encounter.admitted_at, Encounter.discharged_at,
                   Encounter.expected_discharge_date, Encounter.first_expected_discharge_date)
            .where(Encounter.status == "discharged", Encounter.discharged_at >= since)):
        if left is None:
            continue
        left_day: date = left.astimezone(zone).date()
        for g in (groups[OVERALL], groups[label(unit_id)]):
            if admitted is not None and admitted <= left:
                g.los.append((left - admitted).total_seconds() / 86400)
            if expected is not None:
                g.vs_expected.append((left_day - expected).days)
                g.final_error.append(abs((left_day - expected).days))
            if first is not None:
                g.first_error.append(abs((left_day - first).days))
                g.edd_changed += expected is not None and first != expected
            g.before_noon.append(left.astimezone(zone).hour < NOON)
            if left > now - timedelta(days=READMISSION_DAYS):
                g.follow_up_pending += 1
            else:
                g.followed.append(any(left < a <= left + timedelta(days=READMISSION_DAYS)
                                      for a in stays.get(patient_id, ())))

    # Delay reasons recorded in the window, attributed to the unit of the stay they belong to.
    delay_counts: dict[str, Counter] = {kind: Counter() for kind in DELAY_KINDS}
    by_unit: dict[str, Counter] = defaultdict(Counter)
    for kind, code, unit_id in await db.execute(
            select(DelayReason.kind, DelayReason.code, Encounter.unit_id)
            .outerjoin(Encounter, Encounter.encounter_id == DelayReason.encounter_id)
            .where(DelayReason.created_at >= since)):
        delay_counts.setdefault(kind, Counter())[code] += 1
        by_unit[label(unit_id)][code] += 1
    all_codes = sum(delay_counts.values(), Counter())

    overall = groups.pop(OVERALL, _Group())
    return {
        "window_days": days,
        "timezone": str(zone),
        "min_group_size": MIN_GROUP_SIZE,
        "overall": overall.view(),
        "by_unit": {unit: g.view() for unit, g in sorted(groups.items())},
        "delay_reasons": {
            "recorded": sum(all_codes.values()),
            "pareto": pareto(all_codes),
            "by_kind": {kind: pareto(c) for kind, c in delay_counts.items()},
            "by_unit": {unit: {"n": sum(c.values()), "by_code": dict(sorted(c.items()))}
                        for unit, c in sorted(by_unit.items())},
        },
        "notes": {
            "attribution": "by the unit of the stay; no service line in the data model yet",
            "first_expected_date": "recorded from migration 0019 on; earlier stays count only toward the final date",
            "readmission": f"another admission within {READMISSION_DAYS} days of discharge, any unit; discharges "
                           f"less than {READMISSION_DAYS} days ago are pending follow-up",
        },
    }

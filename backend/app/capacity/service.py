"""Capacity board (ADR-002 item 17): expected discharges by unit and hour, discharge before noon, and the beds
waiting to be cleaned.

The board is an operational view for the people who run the units, so it shows small numbers as they are (a charge
nurse needs to know about the one discharge expected at 10:00). It carries no patient-level data: counts per unit and
hour, and beds as locations with times. Which rows a user sees follows unit scope (ADR-002 item 3): RLS on Postgres
(encounters through their patient, bed turnovers by unit), and the unit list itself is limited to the user's units.

"Expected" comes from the discharge plan (`encounters.expected_discharge_date`, item 8). The plan has a date and no
time, so the hour is the active workflow's `estimated_discharge_time` when it falls on that day; otherwise the
discharge is counted as `unscheduled`. Days and hours are hospital time (HOSPITAL_TIMEZONE).
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import write_audit
from app.capacity import beds
from app.capacity.models import OPEN_BED_STATUSES, BedTurnover
from app.core.db import utcnow
from app.core.security import CurrentUser
from app.discharge.models import ACTIVE_WORKFLOW_STATUSES, Workflow
from app.discharge.templates import hospital_today, hospital_zone
from app.identity.models import Unit
from app.patients.models import Encounter

NOON = 12
BEFORE_NOON_DAYS = 7
# Beds made available this long ago still show on the board, so the unit sees what just came back into service.
RECENT_AVAILABLE_HOURS = 12
UNASSIGNED = "unassigned"


def _local(value: datetime) -> datetime:
    return value.astimezone(hospital_zone())


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), hospital_zone())
    return start, start + timedelta(days=1)


def _rate(n: int, hits: int) -> Optional[float]:
    return round(hits / n, 3) if n else None


def _visible_units(units: list[Unit], user: CurrentUser) -> list[Unit]:
    if not user.unit_scoped:
        return units
    return [u for u in units if u.unit_id in set(user.unit_ids)]


def _bed_view(t: BedTurnover, now: datetime) -> dict:
    end = t.available_at or now
    return {**t.to_dict(), "minutes_out_of_service": round((end - t.dirty_at).total_seconds() / 60)}


async def board(db: AsyncSession, user: CurrentUser, day: Optional[date] = None) -> dict:
    now = utcnow()
    today = hospital_today()
    day = day or today
    start, end = _day_bounds(day)
    units = _visible_units(sorted(await db.scalars(select(Unit)), key=lambda u: u.code), user)
    allowed = {u.unit_id for u in units}

    def key_of(unit_id: Optional[str]) -> Optional[str]:
        """The board column for a row: its unit, None (unassigned), or skip ("") for a unit the user isn't on."""
        if unit_id is None:
            return None
        return unit_id if unit_id in allowed else ""

    columns: dict[Optional[str], dict] = {}

    def column(unit_id: Optional[str]) -> dict:
        if unit_id not in columns:
            unit = next((u for u in units if u.unit_id == unit_id), None)
            columns[unit_id] = {
                "unit_id": unit_id, "unit_code": unit.code if unit else None,
                "unit_name": unit.name if unit else "Unassigned",
                "in_house": 0, "expected": 0, "expected_by_hour": defaultdict(int), "unscheduled": 0,
                "expected_tomorrow": 0, "past_expected_date": 0, "no_expected_date": 0,
                "discharged": 0, "discharged_by_hour": defaultdict(int), "discharged_before_noon": 0,
                "beds_dirty": 0, "beds_cleaning": 0, "beds_available": 0,
            }
        return columns[unit_id]

    for unit in units:
        column(unit.unit_id)

    estimated = {wf.encounter_id: wf.estimated_discharge_time for wf in await db.scalars(
        select(Workflow).where(Workflow.status.in_(ACTIVE_WORKFLOW_STATUSES), Workflow.encounter_id.is_not(None)))}
    for enc in await db.scalars(select(Encounter).where(Encounter.status == "active")):
        k = key_of(enc.unit_id)
        if k == "":
            continue
        col = column(k)
        col["in_house"] += 1
        expected = enc.expected_discharge_date
        if expected is None:
            col["no_expected_date"] += 1
        elif expected == day:
            col["expected"] += 1
            at = estimated.get(enc.encounter_id)
            if at is not None and start <= at < end:
                col["expected_by_hour"][_local(at).hour] += 1
            else:
                col["unscheduled"] += 1
        elif expected == day + timedelta(days=1):
            col["expected_tomorrow"] += 1
        elif expected < day and day == today:
            col["past_expected_date"] += 1

    # Discharged that day, and the before-noon rate over the last BEFORE_NOON_DAYS days up to it.
    window_start = start - timedelta(days=BEFORE_NOON_DAYS - 1)
    window: dict[Optional[str], list[bool]] = defaultdict(list)
    for unit_id, left in await db.execute(select(Encounter.unit_id, Encounter.discharged_at).where(
            Encounter.status == "discharged", Encounter.discharged_at >= window_start, Encounter.discharged_at < end)):
        k = key_of(unit_id)
        if k == "" or left is None:
            continue
        local = _local(left)
        window[k].append(local.hour < NOON)
        if left >= start:
            col = column(k)
            col["discharged"] += 1
            col["discharged_by_hour"][local.hour] += 1
            col["discharged_before_noon"] += local.hour < NOON

    bed_rows = []
    turnaround: dict[Optional[str], list[float]] = defaultdict(list)
    recent = now - timedelta(hours=RECENT_AVAILABLE_HOURS)
    for t in await db.scalars(select(BedTurnover).where(
            (BedTurnover.status.in_(OPEN_BED_STATUSES)) |
            ((BedTurnover.status == "available") & (BedTurnover.available_at >= window_start)))
            .order_by(BedTurnover.dirty_at)):
        k = key_of(t.unit_id)
        if k == "":
            continue
        col = column(k)
        if t.status == "available" and t.available_at is not None:
            turnaround[k].append((t.available_at - t.dirty_at).total_seconds() / 60)
            if t.available_at < recent:
                continue
            col["beds_available"] += 1
        else:
            col[f"beds_{t.status}"] += 1
        bed_rows.append(_bed_view(t, now))

    out = []
    for k, col in sorted(columns.items(), key=lambda kv: (kv[0] is None, kv[1]["unit_code"] or "")):
        flags = window.get(k, [])
        minutes = turnaround.get(k, [])
        col["expected_by_hour"] = {str(h): n for h, n in sorted(col["expected_by_hour"].items())}
        col["discharged_by_hour"] = {str(h): n for h, n in sorted(col["discharged_by_hour"].items())}
        col["before_noon_7d"] = {"n": len(flags), "before_noon": sum(flags), "rate": _rate(len(flags), sum(flags))}
        col["turnaround_minutes_7d"] = {"n": len(minutes),
                                        "median": round(median(minutes)) if minutes else None}
        out.append(col)

    all_flags = [f for flags in window.values() for f in flags]
    totals = {key: sum(c[key] for c in out) for key in (
        "in_house", "expected", "unscheduled", "expected_tomorrow", "past_expected_date", "no_expected_date",
        "discharged", "discharged_before_noon", "beds_dirty", "beds_cleaning", "beds_available")}
    hours: dict[str, int] = defaultdict(int)
    for c in out:
        for h, n in c["expected_by_hour"].items():
            hours[h] += n
    totals["expected_by_hour"] = dict(sorted(hours.items(), key=lambda kv: int(kv[0])))
    totals["before_noon_7d"] = {"n": len(all_flags), "before_noon": sum(all_flags),
                                "rate": _rate(len(all_flags), sum(all_flags))}
    return {
        "date": day.isoformat(), "today": today.isoformat(), "timezone": str(hospital_zone()),
        "noon_hour": NOON, "units": out, "totals": totals,
        "beds": sorted(bed_rows, key=lambda b: (b["status"] == "available", b["dirty_at"])),
        "unit_scoped": user.unit_scoped,
    }


async def list_beds(db: AsyncSession, status: Optional[str] = None) -> list[dict]:
    stmt = select(BedTurnover).order_by(BedTurnover.dirty_at)
    stmt = stmt.where(BedTurnover.status == status) if status else stmt.where(
        BedTurnover.status.in_(OPEN_BED_STATUSES))
    now = utcnow()
    return [_bed_view(t, now) for t in await db.scalars(stmt.limit(500))]


async def _unit_ok(db: AsyncSession, actor: CurrentUser, unit_id: Optional[str]) -> None:
    if unit_id is not None and await db.get(Unit, unit_id) is None:
        raise HTTPException(status_code=400, detail="Unknown unit")
    if actor.unit_scoped and unit_id is not None and unit_id not in actor.unit_ids:
        raise HTTPException(status_code=403, detail="That unit is not one of yours")


async def report_dirty(db: AsyncSession, actor: CurrentUser, unit_id: Optional[str], room_number: str,
                       request: Optional[Request] = None) -> dict:
    """A bed that needs cleaning without a discharge the system saw (a move not on the ADT feed, a spill)."""
    await _unit_ok(db, actor, unit_id)
    existing = await beds.open_turnover(db, unit_id, room_number.strip())
    if existing is not None:
        return _bed_view(existing, utcnow())  # already waiting to be cleaned
    turnover = await beds.mark_dirty(db, unit_id, room_number, source="manual")
    if turnover is None:
        raise HTTPException(status_code=400, detail="room_number is required")
    await write_audit(db, actor, "BED_DIRTY", "bed", turnover.turnover_id,
                      f"unit={unit_id or UNASSIGNED} source=manual", request)
    return _bed_view(turnover, utcnow())


# Allowed moves. dirty -> available records a clean whose start nobody entered.
TRANSITIONS = {"dirty": ("cleaning", "available"), "cleaning": ("available",)}


async def advance(db: AsyncSession, actor: CurrentUser, turnover_id: str, status: str,
                  request: Optional[Request] = None) -> dict:
    turnover = await db.get(BedTurnover, turnover_id)
    if turnover is None:
        raise HTTPException(status_code=404, detail="Bed turnover not found")
    await _unit_ok(db, actor, turnover.unit_id)
    if status not in TRANSITIONS.get(turnover.status, ()):
        raise HTTPException(status_code=409, detail=f"A {turnover.status} bed cannot become {status}")
    now = utcnow()
    previous = turnover.status
    turnover.status = status
    if status == "cleaning":
        turnover.cleaning_started_at, turnover.cleaning_started_by = now, actor.user_id
    else:
        turnover.available_at, turnover.available_by = now, actor.user_id
    await db.flush()
    await write_audit(db, actor, "BED_STATUS", "bed", turnover.turnover_id,
                      f"unit={turnover.unit_id or UNASSIGNED} {previous}->{status}", request)
    return _bed_view(turnover, now)

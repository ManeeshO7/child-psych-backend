"""
Doctor availability: blocks define "when I'm available"; doctor then picks which
slots to offer per duration (30/45/75 min). Patients only see offered (bookable) slots.
"""
from datetime import date, datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from prisma import fields as prisma_fields

from app.db import get_db
from app.dependencies import require_doctor

# Practice timezone for availability and slot display
PRACTICE_TZ = ZoneInfo("America/Los_Angeles")
SLOT_MINUTES = 30
# How far ahead patients can see and doctors can configure availability.
AVAILABILITY_DAYS_AHEAD = 90  # 3 months

router = APIRouter(prefix="/api/availability", tags=["availability"])


ALLOWED_DURATION_OPTIONS = (30, 45, 75)


class AvailabilityBlockIn(BaseModel):
    date: str  # "YYYY-MM-DD"
    startTime: str  # "09:00"
    endTime: str  # "12:00"
    allowedDurationsMinutes: list[int] | None = None  # e.g. [30, 45, 60]; null = all


class OfferedSlotsIn(BaseModel):
    date: str  # "YYYY-MM-DD"
    durationMinutes: int  # 30, 45, or 75
    startTimes: list[str]  # ["09:00", "09:30", ...] (HH:MM, 15-min aligned)


def _parse_time(s: str) -> time:
    """Parse 'HH:MM' or 'H:MM' to time."""
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time: {s}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {s}")
    return time(hour=h, minute=m)


def _parse_date(s: str) -> date:
    """Parse 'YYYY-MM-DD' or ISO datetime string to date."""
    s = s.strip()
    if "T" in s:
        # Full ISO datetime from DB or frontend (e.g. 2026-02-05T00:00:00.000Z)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    return date.fromisoformat(s)


def _block_to_slot_starts(block_date: date, start_time: time, end_time: time):
    """Yield (date, time) for each 30-min slot start within the block (end exclusive)."""
    # Convert to minutes since midnight for easier iteration
    start_m = start_time.hour * 60 + start_time.minute
    end_m = end_time.hour * 60 + end_time.minute
    if start_m >= end_m:
        return
    t = start_m
    while t + SLOT_MINUTES <= end_m:
        h, m = divmod(t, 60)
        yield block_date, time(hour=h, minute=m)
        t += SLOT_MINUTES


def _slot_to_datetime(block_date: date, slot_start: time) -> datetime:
    """Slot start as datetime in practice TZ (for comparison and API response)."""
    return datetime.combine(block_date, slot_start, tzinfo=PRACTICE_TZ)


def _get_availability_window() -> tuple[date, date]:
    """Return (start_date, end_date) window for availability in practice TZ."""
    today = datetime.now(PRACTICE_TZ).date()
    # Inclusive range: today .. today + (N-1)
    return today, today + timedelta(days=AVAILABILITY_DAYS_AHEAD - 1)


def _date_range_iso(start_d: date, end_d: date) -> dict:
    """Return Prisma-friendly date filter (ISO-8601 strings)."""
    return {
        "gte": f"{start_d.isoformat()}T00:00:00.000Z",
        "lte": f"{end_d.isoformat()}T23:59:59.999Z",
    }


async def get_available_slot_set(db, doctor_id: str) -> set[tuple[date, time]]:
    """
    Return set of (date, time) in practice TZ for each available 30-min slot.
    Used by appointments router to validate booking.
    """
    start_d, end_d = _get_availability_window()
    blocks = await db.doctoravailability.find_many(
        where={
            "doctorId": doctor_id,
            "date": _date_range_iso(start_d, end_d),
        },
    )
    candidate_slots = []
    for b in blocks:
        d = getattr(b, "date", None)
        if d is None:
            continue
        # Normalize to date so slot tuples are (date, time) and match slot_key at booking
        block_date = d.date() if isinstance(d, datetime) else (d if hasattr(d, "year") else _parse_date(str(d)[:10]))
        if not isinstance(block_date, date):
            block_date = _parse_date(str(block_date)[:10])
        start_t = _parse_time(b.startTime)
        end_t = _parse_time(b.endTime)
        for slot_date, slot_start in _block_to_slot_starts(block_date, start_t, end_t):
            dt = _slot_to_datetime(slot_date, slot_start)
            candidate_slots.append((slot_date, slot_start))
    if not candidate_slots:
        return set()
    now = datetime.now(PRACTICE_TZ)
    window_start = datetime.combine(start_d, time(0, 0), tzinfo=PRACTICE_TZ)
    window_end = datetime.combine(end_d, time(23, 59), tzinfo=PRACTICE_TZ)
    appointments = await db.appointment.find_many(
        where={
            "doctorId": doctor_id,
            "status": {"not": "cancelled"},
            "scheduledAt": {"gte": window_start, "lte": window_end},
        },
    )
    booked_starts = set()
    for a in appointments:
        s = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at")
        if s:
            if s.tzinfo is None:
                s = s.replace(tzinfo=PRACTICE_TZ)
            else:
                s = s.astimezone(PRACTICE_TZ)
            # Normalize to (date, time(hour, minute)) so slot comparison matches 30-min slots
            booked_starts.add((s.date(), time(s.hour, s.minute)))
    return {(d, t) for (d, t) in candidate_slots if (d, t) not in booked_starts and datetime.combine(d, t, tzinfo=PRACTICE_TZ) >= now}


def _time_to_hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def _candidate_slots_in_memory(
    blocks_for_day: list,
    appointments: list,
    day: date,
    duration_minutes: int,
) -> list[tuple[datetime, datetime]]:
    """Compute candidate (start_dt, end_dt) for `day` and `duration_minutes` from in-memory blocks and appointments."""
    step_minutes = 15
    existing_intervals: list[tuple[datetime, datetime]] = []
    for a in appointments:
        s = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at")
        if not s:
            continue
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc).astimezone(PRACTICE_TZ)
        else:
            s = s.astimezone(PRACTICE_TZ)
        if s.date() != day:
            continue
        dur = getattr(a, "durationMinutes", None) or getattr(a, "duration_minutes", SLOT_MINUTES)
        existing_intervals.append((s, s + timedelta(minutes=dur)))

    def overlaps_any(start: datetime, end: datetime) -> bool:
        for a_start, a_end in existing_intervals:
            if start < a_end and a_start < end:
                return True
        return False

    now = datetime.now(PRACTICE_TZ)
    out: list[tuple[datetime, datetime]] = []
    for b in blocks_for_day:
        allowed = getattr(b, "allowedDurationsMinutes", None) if hasattr(b, "allowedDurationsMinutes") else (b.get("allowedDurationsMinutes") if isinstance(b, dict) else None)
        if allowed is not None:
            if not isinstance(allowed, list):
                allowed = list(allowed) if allowed else []
            if allowed and duration_minutes not in allowed:
                continue
        start_t = _parse_time(b.startTime if hasattr(b, "startTime") else b["startTime"])
        end_t = _parse_time(b.endTime if hasattr(b, "endTime") else b["endTime"])
        block_start_dt = datetime.combine(day, start_t, tzinfo=PRACTICE_TZ)
        block_end_dt = datetime.combine(day, end_t, tzinfo=PRACTICE_TZ)
        t = block_start_dt
        while t + timedelta(minutes=duration_minutes) <= block_end_dt:
            end_dt = t + timedelta(minutes=duration_minutes)
            if t >= now and not overlaps_any(t, end_dt):
                out.append((t, end_dt))
            t += timedelta(minutes=step_minutes)
    return out


async def _candidate_slots_for_date_duration(db, doctor_id: str, day: date, duration_minutes: int):
    """Yield (start_dt, end_dt) for each possible slot on `day` with `duration_minutes` from blocks, excluding booked."""
    step_minutes = 15
    blocks = await db.doctoravailability.find_many(
        where={
            "doctorId": doctor_id,
            "date": {"gte": f"{day.isoformat()}T00:00:00.000Z", "lte": f"{day.isoformat()}T23:59:59.999Z"},
        },
        order=[{"startTime": "asc"}],
    )
    appointments = await db.appointment.find_many(
        where={
            "doctorId": doctor_id,
            "status": {"not": "cancelled"},
            "scheduledAt": {
                "gte": datetime.combine(day, time(0, 0), tzinfo=PRACTICE_TZ),
                "lte": datetime.combine(day, time(23, 59), tzinfo=PRACTICE_TZ),
            },
        },
    )
    existing_intervals: list[tuple[datetime, datetime]] = []
    for a in appointments:
        s = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at")
        if not s:
            continue
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc).astimezone(PRACTICE_TZ)
        else:
            s = s.astimezone(PRACTICE_TZ)
        dur = getattr(a, "durationMinutes", None) or getattr(a, "duration_minutes", SLOT_MINUTES)
        existing_intervals.append((s, s + timedelta(minutes=dur)))

    def overlaps_any(start: datetime, end: datetime) -> bool:
        for a_start, a_end in existing_intervals:
            if start < a_end and a_start < end:
                return True
        return False

    now = datetime.now(PRACTICE_TZ)
    out: list[tuple[datetime, datetime]] = []
    for b in blocks:
        allowed = getattr(b, "allowedDurationsMinutes", None)
        if allowed is not None:
            if not isinstance(allowed, list):
                allowed = list(allowed) if allowed else []
            if allowed and duration_minutes not in allowed:
                continue
        block_date = getattr(b, "date", None)
        if block_date is None:
            continue
        block_date = block_date.date() if isinstance(block_date, datetime) else _parse_date(str(block_date)[:10])
        start_t = _parse_time(b.startTime)
        end_t = _parse_time(b.endTime)
        block_start_dt = datetime.combine(block_date, start_t, tzinfo=PRACTICE_TZ)
        block_end_dt = datetime.combine(block_date, end_t, tzinfo=PRACTICE_TZ)
        t = block_start_dt
        while t + timedelta(minutes=duration_minutes) <= block_end_dt:
            end_dt = t + timedelta(minutes=duration_minutes)
            if t >= now and not overlaps_any(t, end_dt):
                out.append((t, end_dt))
            t += timedelta(minutes=step_minutes)
    return out


@router.get("/")
async def list_availability(session: dict = Depends(require_doctor)):
    """List current doctor availability blocks for the configured window (e.g. next 3 months)."""
    db = await get_db()
    start_d, end_d = _get_availability_window()
    blocks = await db.doctoravailability.find_many(
        where={
            "doctorId": session["id"],
            "date": _date_range_iso(start_d, end_d),
        },
        order=[{"date": "asc"}, {"startTime": "asc"}],
    )
    out = []
    for b in blocks:
        d = getattr(b, "date", None)
        if hasattr(d, "isoformat"):
            d_str = d.isoformat()[:10]  # Return date-only so frontend and PUT body stay consistent
        else:
            d_str = str(d)[:10]
        allowed = getattr(b, "allowedDurationsMinutes", None)
        if allowed is not None and not isinstance(allowed, list):
            allowed = list(allowed) if allowed else None
        out.append({
            "id": b.id,
            "date": d_str,
            "startTime": b.startTime,
            "endTime": b.endTime,
            "allowedDurationsMinutes": allowed,
        })
    return {"blocks": out}


@router.put("/")
async def set_availability(
    body: list[AvailabilityBlockIn],
    session: dict = Depends(require_doctor),
):
    """Replace doctor availability for the configured window with the given blocks."""
    db = await get_db()
    start_d, end_d = _get_availability_window()
    for b in body:
        d = _parse_date(b.date)
        if not (start_d <= d <= end_d):
            raise HTTPException(
                status_code=400,
                detail=f"Date {b.date} must be within the next {AVAILABILITY_DAYS_AHEAD} days ({start_d} to {end_d})",
            )
        start_t = _parse_time(b.startTime)
        end_t = _parse_time(b.endTime)
        if start_t >= end_t:
            raise HTTPException(
                status_code=400,
                detail=f"startTime must be before endTime for {b.date}",
            )
        if b.allowedDurationsMinutes is not None:
            for dur in b.allowedDurationsMinutes:
                if dur not in ALLOWED_DURATION_OPTIONS:
                    raise HTTPException(
                        status_code=400,
                        detail=f"allowedDurationsMinutes must only contain {ALLOWED_DURATION_OPTIONS}",
                    )
    # Delete existing blocks in range, then create new ones in one batch (Prisma expects full ISO-8601 DateTime).
    await db.doctoravailability.delete_many(
        where={
            "doctorId": session["id"],
            "date": _date_range_iso(start_d, end_d),
        },
    )
    availability_to_create: list[dict] = []
    for b in body:
        d = _parse_date(b.date)
        if not (start_d <= d <= end_d):
            continue
        row: dict = {
            "doctorId": session["id"],
            "date": f"{d.isoformat()}T00:00:00.000Z",
            "startTime": b.startTime.strip(),
            "endTime": b.endTime.strip(),
        }
        if b.allowedDurationsMinutes is not None:
            row["allowedDurationsMinutes"] = prisma_fields.Json(data=b.allowedDurationsMinutes)
        availability_to_create.append(row)
    if availability_to_create:
        await db.doctoravailability.create_many(data=availability_to_create)

    # Auto-save offered slots: set offered = all candidate slots for dates in body (one delete, one appointments load, one create_many).
    dates_in_body = {_parse_date(b.date) for b in body if start_d <= _parse_date(b.date) <= end_d}
    window_start = datetime.combine(start_d, time(0, 0), tzinfo=PRACTICE_TZ)
    window_end = datetime.combine(end_d, time(23, 59), tzinfo=PRACTICE_TZ)
    appointments = await db.appointment.find_many(
        where={
            "doctorId": session["id"],
            "status": {"not": "cancelled"},
            "scheduledAt": {"gte": window_start, "lte": window_end},
        },
    )
    await db.doctorbookableslot.delete_many(
        where={
            "doctorId": session["id"],
            "date": _date_range_iso(start_d, end_d),
        },
    )
    slots_to_create: list[dict] = []
    for d in dates_in_body:
        blocks_for_day = [b for b in body if _parse_date(b.date) == d]
        allowed = set()
        for b in blocks_for_day:
            if b.allowedDurationsMinutes:
                allowed.update(b.allowedDurationsMinutes)
            else:
                allowed.update(ALLOWED_DURATION_OPTIONS)
        for dur in allowed:
            pairs = _candidate_slots_in_memory(blocks_for_day, appointments, d, dur)
            start_times = list(dict.fromkeys(_time_to_hhmm(s.time()) for s, _ in pairs))
            date_iso = f"{d.isoformat()}T00:00:00.000Z"
            for st in start_times:
                slots_to_create.append({
                    "doctorId": session["id"],
                    "date": date_iso,
                    "startTime": st.strip(),
                    "durationMinutes": dur,
                })
    if slots_to_create:
        await db.doctorbookableslot.create_many(data=slots_to_create)

    return await list_availability(session)


@router.get("/candidate-slots")
async def get_candidate_slots(
    date: str,  # query param: date=YYYY-MM-DD
    durationMinutes: int,
    session: dict = Depends(require_doctor),
):
    """
    For the doctor: return possible slot start times on the given date for the given duration,
    derived from availability blocks (excluding already-booked). Doctor uses these to choose which to offer.
    """
    if durationMinutes not in ALLOWED_DURATION_OPTIONS:
        raise HTTPException(status_code=400, detail="durationMinutes must be 30, 45, or 75")
    day = _parse_date(date)
    start_d, end_d = _get_availability_window()
    if not (start_d <= day <= end_d):
        raise HTTPException(status_code=400, detail=f"Date must be between {start_d} and {end_d}")
    db = await get_db()
    pairs = await _candidate_slots_for_date_duration(db, session["id"], day, durationMinutes)
    slots = [
        {"startTime": _time_to_hhmm(s.time()), "endTime": _time_to_hhmm(e.time())}
        for s, e in pairs
    ]
    return {"slots": slots}


@router.get("/offered-slots")
async def get_offered_slots(
    date: str,  # query param: date=YYYY-MM-DD
    durationMinutes: int,
    session: dict = Depends(require_doctor),
):
    """Return the start times the doctor has already offered for this date and duration."""
    if durationMinutes not in ALLOWED_DURATION_OPTIONS:
        raise HTTPException(status_code=400, detail="durationMinutes must be 30, 45, or 75")
    day = _parse_date(date)
    start_d, end_d = _get_availability_window()
    if not (start_d <= day <= end_d):
        raise HTTPException(status_code=400, detail=f"Date must be between {start_d} and {end_d}")
    db = await get_db()
    rows = await db.doctorbookableslot.find_many(
        where={
            "doctorId": session["id"],
            "date": {"gte": f"{day.isoformat()}T00:00:00.000Z", "lte": f"{day.isoformat()}T23:59:59.999Z"},
            "durationMinutes": durationMinutes,
        },
        order=[{"startTime": "asc"}],
    )
    start_times = [getattr(r, "startTime", None) or getattr(r, "start_time") for r in rows if getattr(r, "startTime", None) or getattr(r, "start_time")]
    return {"startTimes": start_times}


@router.put("/offered-slots")
async def set_offered_slots(body: OfferedSlotsIn, session: dict = Depends(require_doctor)):
    """
    Set which slots are offered for the given date and duration. Replaces any existing offered slots
    for that (doctor, date, duration). startTimes must be 15-min aligned.
    """
    if body.durationMinutes not in ALLOWED_DURATION_OPTIONS:
        raise HTTPException(status_code=400, detail="durationMinutes must be 30, 45, or 75")
    day = _parse_date(body.date)
    start_d, end_d = _get_availability_window()
    if not (start_d <= day <= end_d):
        raise HTTPException(status_code=400, detail=f"Date must be between {start_d} and {end_d}")
    for st in body.startTimes:
        _parse_time(st)  # validate
        slot_min = int(st.split(":")[0]) * 60 + int(st.split(":")[1])
        if slot_min % 15 != 0:
            raise HTTPException(status_code=400, detail=f"startTime {st} must be 15-minute aligned")
    db = await get_db()
    await db.doctorbookableslot.delete_many(
        where={
            "doctorId": session["id"],
            "date": {"gte": f"{day.isoformat()}T00:00:00.000Z", "lte": f"{day.isoformat()}T23:59:59.999Z"},
            "durationMinutes": body.durationMinutes,
        },
    )
    for st in body.startTimes:
        await db.doctorbookableslot.create(
            data={
                "doctor": {"connect": {"id": session["id"]}},
                "date": f"{day.isoformat()}T00:00:00.000Z",
                "startTime": st.strip(),
                "durationMinutes": body.durationMinutes,
            },
        )
    rows = await db.doctorbookableslot.find_many(
        where={
            "doctorId": session["id"],
            "date": {"gte": f"{day.isoformat()}T00:00:00.000Z", "lte": f"{day.isoformat()}T23:59:59.999Z"},
            "durationMinutes": body.durationMinutes,
        },
        order=[{"startTime": "asc"}],
    )
    start_times = [getattr(r, "startTime", None) or getattr(r, "start_time") for r in rows if getattr(r, "startTime", None) or getattr(r, "start_time")]
    return {"startTimes": start_times}


@router.get("/slots")
async def get_available_slots(
    durationMinutes: int | None = None,
    excludeAppointmentId: str | None = None,
):
    """
    Return bookable slots for patients. Only slots the doctor has explicitly offered
    for this duration are returned; booked slots are excluded.

    - durationMinutes: 30, 45, or 75. Defaults to SLOT_MINUTES when not provided.
    - excludeAppointmentId: when rescheduling, exclude this appointment so its current slot appears available.
    """
    db = await get_db()
    doctor = await db.user.find_first(where={"role": "doctor"})
    if not doctor:
        return {"slots": []}

    requested_duration = durationMinutes or SLOT_MINUTES
    if requested_duration <= 0:
        raise HTTPException(status_code=400, detail="durationMinutes must be positive")

    start_d, end_d = _get_availability_window()
    bookable = await db.doctorbookableslot.find_many(
        where={
            "doctorId": doctor.id,
            "date": _date_range_iso(start_d, end_d),
            "durationMinutes": requested_duration,
        },
        order=[{"date": "asc"}, {"startTime": "asc"}],
    )
    if not bookable:
        return {"slots": []}

    window_start = datetime.combine(start_d, time(0, 0), tzinfo=PRACTICE_TZ)
    window_end = datetime.combine(end_d, time(23, 59), tzinfo=PRACTICE_TZ)
    appointments = await db.appointment.find_many(
        where={
            "doctorId": doctor.id,
            "status": {"not": "cancelled"},
            "scheduledAt": {"gte": window_start, "lte": window_end},
        },
    )
    existing_intervals: list[tuple[datetime, datetime]] = []
    for a in appointments:
        if excludeAppointmentId and a.id == excludeAppointmentId:
            continue
        s = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at")
        if not s:
            continue
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc).astimezone(PRACTICE_TZ)
        else:
            s = s.astimezone(PRACTICE_TZ)
        dur = getattr(a, "durationMinutes", None) or getattr(a, "duration_minutes", SLOT_MINUTES)
        existing_intervals.append((s, s + timedelta(minutes=dur)))

    def overlaps_any(start: datetime, end: datetime) -> bool:
        for a_start, a_end in existing_intervals:
            if start < a_end and a_start < end:
                return True
        return False

    now = datetime.now(PRACTICE_TZ)
    result_slots: list[dict] = []
    for row in bookable:
        d = getattr(row, "date", None)
        if d is None:
            continue
        block_date = d.date() if isinstance(d, datetime) else _parse_date(str(d)[:10])
        start_t = _parse_time(getattr(row, "startTime", None) or getattr(row, "start_time", ""))
        start_dt = datetime.combine(block_date, start_t, tzinfo=PRACTICE_TZ)
        end_dt = start_dt + timedelta(minutes=requested_duration)
        if start_dt >= now and not overlaps_any(start_dt, end_dt):
            result_slots.append({"start": start_dt.isoformat(), "end": end_dt.isoformat()})

    return {"slots": result_slots}

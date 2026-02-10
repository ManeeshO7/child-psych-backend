"""
Doctor availability (Option A): blocks stored in DB for next 7 days.
Slots are computed as 30-min intervals from blocks; booked slots are excluded.
"""
from datetime import date, datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import get_db
from app.dependencies import require_doctor

# Practice timezone for availability and slot display
PRACTICE_TZ = ZoneInfo("America/Los_Angeles")
SLOT_MINUTES = 30
# How far ahead patients can see and doctors can configure availability.
AVAILABILITY_DAYS_AHEAD = 60

router = APIRouter(prefix="/api/availability", tags=["availability"])


class AvailabilityBlockIn(BaseModel):
    date: str  # "YYYY-MM-DD"
    startTime: str  # "09:00"
    endTime: str  # "12:00"


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


@router.get("/")
async def list_availability(session: dict = Depends(require_doctor)):
    """List current doctor availability blocks for the configured window (e.g. next 60 days)."""
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
        out.append({
            "id": b.id,
            "date": d_str,
            "startTime": b.startTime,
            "endTime": b.endTime,
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
    # Delete existing blocks in range, then create new ones (Prisma expects full ISO-8601 DateTime)
    await db.doctoravailability.delete_many(
        where={
            "doctorId": session["id"],
            "date": _date_range_iso(start_d, end_d),
        },
    )
    for b in body:
        d = _parse_date(b.date)
        if not (start_d <= d <= end_d):
            continue
        await db.doctoravailability.create(
            data={
                "doctorId": session["id"],
                "date": f"{d.isoformat()}T00:00:00.000Z",
                "startTime": b.startTime.strip(),
                "endTime": b.endTime.strip(),
            },
        )
    return await list_availability(session)


@router.get("/slots")
async def get_available_slots():
    """
    Return 30-min bookable slots for the configured availability window
    (e.g. next 60 days). No auth required so patients can see slots.
    """
    db = await get_db()
    doctor = await db.user.find_first(where={"role": "doctor"})
    if not doctor:
        return {"slots": []}
    start_d, end_d = _get_availability_window()
    blocks = await db.doctoravailability.find_many(
        where={
            "doctorId": doctor.id,
            "date": _date_range_iso(start_d, end_d),
        },
        order=[{"date": "asc"}, {"startTime": "asc"}],
    )
    # Build all candidate slots from blocks
    candidate_slots = []
    for b in blocks:
        d = getattr(b, "date", None)
        if d is None:
            continue
        # Normalize to date so slot keys are consistent (date, time)
        block_date = d.date() if isinstance(d, datetime) else (d if hasattr(d, "year") else _parse_date(str(d)[:10]))
        if not isinstance(block_date, date):
            block_date = _parse_date(str(block_date)[:10])
        start_t = _parse_time(b.startTime)
        end_t = _parse_time(b.endTime)
        for slot_date, slot_start in _block_to_slot_starts(block_date, start_t, end_t):
            dt = _slot_to_datetime(slot_date, slot_start)
            candidate_slots.append(dt)
    if not candidate_slots:
        return {"slots": []}
    # Exclude past slots (in practice TZ)
    now = datetime.now(PRACTICE_TZ)
    candidate_slots = [dt for dt in candidate_slots if dt >= now]
    # Load existing appointments for this doctor in the window
    window_start = datetime.combine(start_d, time(0, 0), tzinfo=PRACTICE_TZ)
    window_end = datetime.combine(end_d, time(23, 59), tzinfo=PRACTICE_TZ)
    appointments = await db.appointment.find_many(
        where={
            "doctorId": doctor.id,
            "status": {"not": "cancelled"},
            "scheduledAt": {"gte": window_start, "lte": window_end},
        },
    )
    booked_starts = set()
    for a in appointments:
        s = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at")
        if s:
            if s.tzinfo is None:
                # DB stores UTC; treat naive as UTC then convert to practice TZ
                s = s.replace(tzinfo=timezone.utc).astimezone(PRACTICE_TZ)
            else:
                s = s.astimezone(PRACTICE_TZ)
            # Normalize to (date, time(hour, minute)) so slot comparison matches 30-min slots
            booked_starts.add((s.date(), time(s.hour, s.minute)))
    # Filter out slots that overlap a booked appointment (same 30-min slot)
    def slot_key(dt: datetime):
        return (dt.date(), dt.time())
    available = [
        dt for dt in candidate_slots
        if slot_key(dt) not in booked_starts
    ]
    return {
        "slots": [
            {"start": dt.isoformat(), "end": (dt + timedelta(minutes=SLOT_MINUTES)).isoformat()}
            for dt in sorted(available)
        ],
    }

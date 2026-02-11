from datetime import date, datetime, timedelta, timezone, time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import os

from app.db import get_db
from app.dependencies import require_patient, require_patient_or_doctor, require_doctor
from app.routers.availability import PRACTICE_TZ, SLOT_MINUTES

router = APIRouter(prefix="/api/appointments", tags=["appointments"])

# Appointment types and phase gating
APPOINTMENT_TYPES = {
    "orientation_consult": {"durationMinutes": 30, "label": "Orientation consultation"},
    "clinical_intake": {"durationMinutes": 60, "label": "Clinical Intake Appointment"},
    "followup_med_30": {"durationMinutes": 30, "label": "Follow-up (med management, 30 min)"},
    "followup_med_therapy_45": {"durationMinutes": 45, "label": "Follow-up (med + therapy, 45 min)"},
}
VALID_APPOINTMENT_TYPES = set(APPOINTMENT_TYPES.keys())


def _get_attr(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _serialize_appointment(a, include_patient=False, include_doctor=False):
    scheduled_at = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at", None)
    duration = getattr(a, "duration_minutes", None) or getattr(a, "durationMinutes", 60)
    meet_link = getattr(a, "meetLink", None) or getattr(a, "meet_link", None)
    out = {
        "id": a.id,
        "scheduledAt": scheduled_at.isoformat() if scheduled_at else None,
        "durationMinutes": duration or 60,
        "status": a.status,
        "type": a.type,
        "notes": a.notes,
        "meetLink": meet_link,
    }
    if include_patient and getattr(a, "patient", None):
        p = a.patient
        out["patient"] = {
            "id": p.id,
            "email": p.email,
            "name": f"{_get_attr(p, 'first_name', 'firstName', default='')} {_get_attr(p, 'last_name', 'lastName', default='')}".strip(),
        }
    if include_doctor and getattr(a, "doctor", None):
        d = a.doctor
        out["doctor"] = {
            "id": d.id,
            "email": d.email,
            "name": f"{_get_attr(d, 'first_name', 'firstName', default='')} {_get_attr(d, 'last_name', 'lastName', default='')}".strip(),
        }
    return out


class CreateAppointmentBody(BaseModel):
    scheduledAt: str
    durationMinutes: int = 60
    type: str = "followup"
    notes: str | None = None


class UpdateAppointmentBody(BaseModel):
    status: str | None = None
    notes: str | None = None


@router.get("/allowed-types")
async def get_allowed_appointment_types(session: dict = Depends(require_patient)):
    """
    Return appointment types the current patient is allowed to book, based on phase:
    - New patients: only orientation_consult (30 min).
    - After orientation + approval + no pending forms: clinical_intake (60 min).
    - After clinical intake: followup_med_30 and followup_med_therapy_45.
    """
    db = await get_db()
    patient_id = session["id"]

    completed = await db.appointment.find_many(
        where={
            "patientId": patient_id,
            "status": {"in": ["completed", "paid"]},
        },
    )
    completed_types = {a.type for a in completed}
    has_orientation = "orientation_consult" in completed_types
    has_intake = "clinical_intake" in completed_types or "intake" in completed_types

    patient_user = await db.user.find_unique(
        where={"id": patient_id},
        include={"patientRequest": True},
    )
    request_status = None
    if getattr(patient_user, "patientRequest", None):
        request_status = getattr(patient_user.patientRequest, "status", None)
    is_approved = request_status == "approved"

    pending_forms_count = await db.patientformassignment.count(
        where={"patientId": patient_id, "status": "pending"},
    )
    no_pending_forms = pending_forms_count == 0

    allowed = []
    if not has_orientation:
        allowed.append({
            "type": "orientation_consult",
            "durationMinutes": APPOINTMENT_TYPES["orientation_consult"]["durationMinutes"],
            "label": APPOINTMENT_TYPES["orientation_consult"]["label"],
        })
    if has_orientation and is_approved and no_pending_forms and not has_intake:
        allowed.append({
            "type": "clinical_intake",
            "durationMinutes": APPOINTMENT_TYPES["clinical_intake"]["durationMinutes"],
            "label": APPOINTMENT_TYPES["clinical_intake"]["label"],
        })
    if has_intake:
        allowed.append({
            "type": "followup_med_30",
            "durationMinutes": APPOINTMENT_TYPES["followup_med_30"]["durationMinutes"],
            "label": APPOINTMENT_TYPES["followup_med_30"]["label"],
        })
        allowed.append({
            "type": "followup_med_therapy_45",
            "durationMinutes": APPOINTMENT_TYPES["followup_med_therapy_45"]["durationMinutes"],
            "label": APPOINTMENT_TYPES["followup_med_therapy_45"]["label"],
        })

    allowed_set = {a["type"] for a in allowed}
    all_types = [
        {"type": k, "durationMinutes": v["durationMinutes"], "label": v["label"]}
        for k, v in APPOINTMENT_TYPES.items()
    ]
    not_allowed_reasons = {}
    if "orientation_consult" not in allowed_set:
        not_allowed_reasons["orientation_consult"] = "You've already completed orientation."
    if "clinical_intake" not in allowed_set:
        if not has_orientation:
            not_allowed_reasons["clinical_intake"] = "Complete your initial evaluation (orientation) first."
        elif has_intake:
            not_allowed_reasons["clinical_intake"] = "You've already completed clinical intake."
        else:
            not_allowed_reasons["clinical_intake"] = "Complete your initial evaluation and get approved first."
    if "followup_med_30" not in allowed_set:
        not_allowed_reasons["followup_med_30"] = "Complete clinical intake first."
    if "followup_med_therapy_45" not in allowed_set:
        not_allowed_reasons["followup_med_therapy_45"] = "Complete clinical intake first."

    return {
        "allowedTypes": allowed,
        "allTypes": all_types,
        "notAllowedReasons": not_allowed_reasons,
    }


@router.get("/")
async def list_appointments(
    session: dict = Depends(require_patient_or_doctor),
    # Backwards-compatible pagination knobs (some clients may still send these)
    page: int | None = None,
    pageSize: int | None = None,
    limit: int | None = None,
):
    db = await get_db()
    role = session["role"]

    # Safety: never return an unbounded number of rows to the browser.
    # Default to 200 if no pagination params are provided.
    take = 200
    skip = 0
    if pageSize is not None:
        try:
            ps = int(pageSize)
        except Exception:
            ps = 25
        ps = max(1, min(100, ps))
        take = ps
        try:
            p = int(page or 0)
        except Exception:
            p = 0
        p = max(0, p)
        skip = p * ps
    elif limit is not None:
        try:
            lim = int(limit)
        except Exception:
            lim = 50
        take = max(1, min(200, lim))

    if role == "patient":
        appointments = await db.appointment.find_many(
            where={"patientId": session["id"]},
            order={"scheduledAt": "desc"},
            take=take,
            skip=skip,
            include={"doctor": True},
        )
        return [_serialize_appointment(a, include_doctor=True) for a in appointments]
    else:
        appointments = await db.appointment.find_many(
            where={"doctorId": session["id"]},
            order={"scheduledAt": "desc"},
            take=take,
            skip=skip,
            include={"patient": True},
        )
        return [_serialize_appointment(a, include_patient=True) for a in appointments]


@router.get("/paged")
async def list_appointments_paged(
    session: dict = Depends(require_patient_or_doctor),
    limit: int = 50,
    cursor: str | None = None,
    view: str | None = None,  # "active" | "completed" | None
    date_str: str | None = None,  # YYYY-MM-DD in practice (PST) timezone
):
    """
    Cursor-based pagination for appointments.

    Response shape:
      { "items": [...], "nextCursor": "appt_id" | null, "hasMore": bool }

    Pass `cursor=<last_seen_id>` to fetch the next page.
    Optional `date_str=YYYY-MM-DD` filters to appointments on that date (PST).
    """
    db = await get_db()
    role = session["role"]
    # Clamp limit to keep UI stable
    try:
        limit_int = int(limit)
    except Exception:
        limit_int = 50
    limit_int = max(1, min(100, limit_int))

    where = {"patientId": session["id"]} if role == "patient" else {"doctorId": session["id"]}
    if view == "completed":
        where = {**where, "status": "completed"}
    elif view == "active":
        where = {**where, "status": {"in": ["scheduled", "card_on_file", "paid"]}}

    if date_str:
        try:
            day = date.fromisoformat(date_str)
            start_practice = datetime.combine(day, time(0, 0), tzinfo=PRACTICE_TZ)
            end_practice = datetime.combine(day, time(23, 59, 59, 999999), tzinfo=PRACTICE_TZ)
            start_utc = start_practice.astimezone(timezone.utc)
            end_utc = end_practice.astimezone(timezone.utc)
            where["scheduledAt"] = {"gte": start_utc, "lte": end_utc}
        except (ValueError, TypeError):
            pass  # ignore invalid date_str
    include = {"doctor": True} if role == "patient" else {"patient": True}
    order = [{"scheduledAt": "desc"}, {"id": "desc"}]

    find_kwargs = {
        "where": where,
        "order": order,
        "include": include,
        "take": limit_int + 1,  # fetch 1 extra to detect hasMore
    }
    if cursor:
        # Skip the cursor item itself so we don't repeat it on the next page
        find_kwargs["cursor"] = {"id": cursor}
        find_kwargs["skip"] = 1

    appointments = await db.appointment.find_many(**find_kwargs)
    has_more = len(appointments) > limit_int
    page = appointments[:limit_int]
    next_cursor = page[-1].id if (has_more and len(page) > 0) else None

    if role == "patient":
        return {
            "items": [_serialize_appointment(a, include_doctor=True) for a in page],
            "nextCursor": next_cursor,
            "hasMore": has_more,
        }
    return {
        "items": [_serialize_appointment(a, include_patient=True) for a in page],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@router.post("/")
async def create_appointment(
    body: CreateAppointmentBody,
    session: dict = Depends(require_patient),
):
    db = await get_db()
    try:
        scheduled_at = datetime.fromisoformat(body.scheduledAt.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid scheduledAt (use ISO format)")
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    scheduled_at_practice = scheduled_at.astimezone(PRACTICE_TZ)
    # Require 15-min slot alignment so we can support 30/45/60-minute sessions
    slot_minutes = scheduled_at_practice.hour * 60 + scheduled_at_practice.minute
    if slot_minutes % 15 != 0 or scheduled_at_practice.second != 0 or scheduled_at_practice.microsecond != 0:
        raise HTTPException(
            status_code=400,
            detail="scheduledAt must be a 15-minute slot start (e.g. 10:00, 10:15, 10:30)",
        )
    doctor = await db.user.find_first(where={"role": "doctor"})
    if not doctor:
        raise HTTPException(status_code=503, detail="No provider available")

    # Validate appointment type and phase gating
    if body.type not in VALID_APPOINTMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type. Must be one of: {sorted(VALID_APPOINTMENT_TYPES)}",
        )
    expected_duration = APPOINTMENT_TYPES[body.type]["durationMinutes"]
    requested_duration = body.durationMinutes or expected_duration
    if requested_duration != expected_duration:
        raise HTTPException(
            status_code=400,
            detail=f"Appointment type {body.type} requires durationMinutes={expected_duration}",
        )

    # Phase gating: ensure patient is allowed to book this type
    completed = await db.appointment.find_many(
        where={"patientId": session["id"], "status": {"in": ["completed", "paid"]}},
    )
    completed_types = {a.type for a in completed}
    has_orientation = "orientation_consult" in completed_types
    has_intake = "clinical_intake" in completed_types or "intake" in completed_types
    patient_user = await db.user.find_unique(where={"id": session["id"]}, include={"patientRequest": True})
    request_status = getattr(patient_user.patientRequest, "status", None) if getattr(patient_user, "patientRequest", None) else None
    is_approved = request_status == "approved"
    pending_forms_count = await db.patientformassignment.count(
        where={"patientId": session["id"], "status": "pending"},
    )
    no_pending_forms = pending_forms_count == 0

    if body.type == "orientation_consult" and has_orientation:
        raise HTTPException(status_code=403, detail="You have already completed orientation. You can book clinical intake or follow-ups.")
    if body.type == "clinical_intake":
        if not has_orientation:
            raise HTTPException(status_code=403, detail="Complete an orientation consultation first.")
        if not is_approved:
            raise HTTPException(status_code=403, detail="Your access must be approved before booking clinical intake.")
        if not no_pending_forms:
            raise HTTPException(status_code=403, detail="Complete your assigned forms before booking clinical intake.")
    if body.type in ("followup_med_30", "followup_med_therapy_45") and not has_intake:
        raise HTTPException(status_code=403, detail="Complete clinical intake before booking follow-ups.")

    # Validate that the requested interval fits inside an availability block
    # and does not overlap existing appointments.
    if requested_duration <= 0:
        raise HTTPException(status_code=400, detail="durationMinutes must be positive")

    candidate_start = scheduled_at_practice
    candidate_end = candidate_start + timedelta(minutes=requested_duration)

    # Cannot book in the past (in practice timezone)
    now_practice = datetime.now(PRACTICE_TZ)
    if candidate_start < now_practice:
        raise HTTPException(status_code=400, detail="Cannot book an appointment in the past")

    day = candidate_start.date()
    day_start = datetime.combine(day, time(0, 0), tzinfo=PRACTICE_TZ)
    day_end = datetime.combine(day, time(23, 59), tzinfo=PRACTICE_TZ)

    # Slot must be one the doctor has offered for this duration (DoctorBookableSlot)
    start_hhmm = candidate_start.strftime("%H:%M")
    offered = await db.doctorbookableslot.find_first(
        where={
            "doctorId": doctor.id,
            "date": {"gte": f"{day.isoformat()}T00:00:00.000Z", "lte": f"{day.isoformat()}T23:59:59.999Z"},
            "startTime": start_hhmm,
            "durationMinutes": requested_duration,
        },
    )
    if not offered:
        raise HTTPException(
            status_code=409,
            detail="This time is no longer available. Please choose another time.",
        )

    # Ensure no overlap with existing appointments for this doctor on that day
    existing_appointments = await db.appointment.find_many(
        where={
            "doctorId": doctor.id,
            "status": {"not": "cancelled"},
            "scheduledAt": {"gte": day_start, "lte": day_end},
        },
    )

    for a in existing_appointments:
        s = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at", None)
        if not s:
            continue
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc).astimezone(PRACTICE_TZ)
        else:
            s = s.astimezone(PRACTICE_TZ)
        dur = getattr(a, "durationMinutes", None) or getattr(a, "duration_minutes", SLOT_MINUTES)
        e = s + timedelta(minutes=dur)
        if candidate_start < e and s < candidate_end:
            raise HTTPException(
                status_code=409,
                detail="This time is no longer available. Please choose another time.",
            )
    
    # Check for pending forms (if patient has completed an intake appointment)
    completed_intake = await db.appointment.find_first(
        where={
            "patientId": session["id"],
            "type": "intake",
            "status": {"in": ["completed", "paid"]},
        },
    )
    if completed_intake:
        # Patient has completed intake, check for pending forms
        pending_forms_count = await db.patientformassignment.count(
            where={
                "patientId": session["id"],
                "status": "pending",
            },
        )
        if pending_forms_count > 0:
            raise HTTPException(
                status_code=403,
                detail=f"You have {pending_forms_count} pending form(s) to complete before booking your next appointment. Please complete them in the Forms & Documents section.",
            )
    
    # Meet link: create via Google Calendar API if doctor has connected Calendar
    patient_user = await db.user.find_unique(where={"id": session["id"]})
    patient_name = "Patient"
    patient_email = None
    if patient_user:
        patient_name = (
            (getattr(patient_user, "firstName", None) or getattr(patient_user, "first_name", ""))
            + " "
            + (getattr(patient_user, "lastName", None) or getattr(patient_user, "last_name", ""))
        ).strip() or "Patient"
        patient_email = getattr(patient_user, "email", None)

    meet_link = None
    refresh_token = getattr(doctor, "googleRefreshToken", None) or getattr(doctor, "google_refresh_token", None)
    calendar_id = getattr(doctor, "googleCalendarId", None) or getattr(doctor, "google_calendar_id", None) or "primary"
    if refresh_token:
        from app.google_calendar import create_calendar_event_with_meet
        end_utc = scheduled_at + timedelta(minutes=body.durationMinutes or SLOT_MINUTES)
        meet_link = create_calendar_event_with_meet(
            refresh_token=refresh_token,
            calendar_id=calendar_id,
            start_utc=scheduled_at,
            end_utc=end_utc,
            summary=f"TelePsych — {patient_name}",
            description="Appointment booked via TelePsych.",
            attendee_email=patient_email,
        )

    # If the patient already has a Stripe customer with a saved card, mark this as card_on_file.
    stripe_customer_id = getattr(patient_user, "stripeCustomerId", None) or getattr(
        patient_user, "stripe_customer_id", None
    )
    status = "card_on_file" if stripe_customer_id else "scheduled"

    appointment = await db.appointment.create(
        data={
            "patientId": session["id"],
            "doctorId": doctor.id,
            "scheduledAt": scheduled_at,
            "durationMinutes": body.durationMinutes or SLOT_MINUTES,
            "type": body.type,
            "notes": body.notes,
            "status": status,
            "meetLink": meet_link,
        },
    )
    # Send confirmation email with Meet link
    if patient_user:
        from app.email_service import send_appointment_confirmation_email
        send_appointment_confirmation_email(
            to=patient_user.email,
            patient_first_name=patient_name,
            scheduled_at_iso=scheduled_at.isoformat(),
            meet_link=meet_link,
        )
    return _serialize_appointment(appointment)


@router.get("/{id}")
async def get_appointment(id: str, session: dict = Depends(require_patient_or_doctor)):
    db = await get_db()
    appointment = await db.appointment.find_unique(
        where={"id": id},
        include={"patient": True, "doctor": True},
    )
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    role = session["role"]
    patient_id = getattr(appointment, "patientId", None) or getattr(appointment, "patient_id", None)
    doctor_id = getattr(appointment, "doctorId", None) or getattr(appointment, "doctor_id", None)
    if role == "patient" and patient_id != session["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment")
    if role == "doctor" and doctor_id != session["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment")
    return _serialize_appointment(
        appointment, include_patient=True, include_doctor=True
    )


@router.patch("/{id}")
async def update_appointment(
    id: str,
    body: UpdateAppointmentBody,
    session: dict = Depends(require_patient_or_doctor),
):
    db = await get_db()
    appointment = await db.appointment.find_unique(where={"id": id})
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    role = session["role"]
    patient_id = getattr(appointment, "patientId", None) or getattr(appointment, "patient_id", None)
    doctor_id = getattr(appointment, "doctorId", None) or getattr(appointment, "doctor_id", None)
    if role == "patient":
        if patient_id != session["id"]:
            raise HTTPException(status_code=403, detail="Not your appointment")
        # Patient can only cancel
        if body.status is not None and body.status != "cancelled":
            raise HTTPException(status_code=403, detail="Patients can only cancel")
    else:
        if doctor_id != session["id"]:
            raise HTTPException(status_code=403, detail="Not your appointment")
    data = {}
    if body.status is not None:
        data["status"] = body.status
    if body.notes is not None:
        data["notes"] = body.notes
    if not data:
        return _serialize_appointment(appointment)
    updated = await db.appointment.update(
        where={"id": id},
        data=data,
        include={"patient": True, "doctor": True},
    )
    return _serialize_appointment(updated, include_patient=True, include_doctor=True)


@router.post("/{id}/charge")
async def charge_appointment(id: str, session: dict = Depends(require_doctor)):
    """Charge the patient's saved card for this appointment (doctor only)."""
    import stripe
    from app.stripe_service import charge_customer, get_visit_price_cents, get_visit_price_id

    if not os.environ.get("STRIPE_SECRET_KEY"):
        raise HTTPException(
            status_code=503,
            detail="Payments are not configured (STRIPE_SECRET_KEY and pip install stripe)",
        )
    db = await get_db()
    appointment = await db.appointment.find_unique(
        where={"id": id},
        include={"patient": True, "doctor": True},
    )
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    doctor_id = getattr(appointment, "doctorId", None) or getattr(appointment, "doctor_id", None)
    if doctor_id != session["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment")
    if appointment.status != "card_on_file":
        raise HTTPException(
            status_code=400,
            detail="Appointment must have card on file to charge (patient should save card first).",
        )
    patient = appointment.patient
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")
    stripe_customer_id = getattr(patient, "stripeCustomerId", None) or getattr(patient, "stripe_customer_id", None)
    if not stripe_customer_id:
        raise HTTPException(status_code=400, detail="Patient has no card on file")
    appointment_type = getattr(appointment, "type", None) or "post_intake"
    price_id = get_visit_price_id(appointment_type)
    if not price_id:
        raise HTTPException(
            status_code=500,
            detail=f"Stripe Price ID not configured for appointment type '{appointment_type}'. "
            f"Set STRIPE_PRICE_ID_{appointment_type.upper().replace('-', '_')} in the backend .env.",
        )
    # amount_cents is only for logging/metadata; Stripe uses price_id for the actual charge
    amount_cents = get_visit_price_cents(appointment_type)
    scheduled_at = getattr(appointment, "scheduledAt", None) or getattr(appointment, "scheduled_at")
    desc = f"TelePsych {appointment_type.replace('_', ' ')} visit — {scheduled_at.date() if scheduled_at else 'appointment'}"
    try:
        charge_customer(
            customer_id=stripe_customer_id,
            amount_cents=amount_cents,
            description=desc,
            metadata={"appointment_id": id, "appointment_type": appointment_type},
            price_id=price_id,
        )
    except stripe.error.CardError as e:
        raise HTTPException(status_code=400, detail=e.user_message or "Card charge failed")
    await db.appointment.update(
        where={"id": id},
        data={"status": "paid"},
    )
    return {"ok": True, "status": "paid", "message": "Payment successful. Receipt sent by Stripe."}


@router.post("/{id}/complete")
async def complete_appointment(id: str, session: dict = Depends(require_doctor)):
    """Mark an appointment as completed (doctor only)."""
    db = await get_db()
    appointment = await db.appointment.find_unique(
        where={"id": id},
        include={"patient": True, "doctor": True},
    )
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    doctor_id = getattr(appointment, "doctorId", None) or getattr(appointment, "doctor_id", None)
    if doctor_id != session["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment")
    # Only allow completing appointments that are paid or card_on_file
    if appointment.status not in ("paid", "card_on_file"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot complete appointment with status '{appointment.status}'. Appointment must be paid first.",
        )
    await db.appointment.update(
        where={"id": id},
        data={"status": "completed"},
    )
    return {"ok": True, "status": "completed", "message": "Appointment marked as completed."}

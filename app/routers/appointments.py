from datetime import date, datetime, timedelta, timezone, time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import os

from app.db import get_db
from app.dependencies import require_patient, require_patient_or_doctor, require_doctor
from app.routers.availability import PRACTICE_TZ, SLOT_MINUTES
from prisma import fields as prisma_fields

router = APIRouter(prefix="/api/appointments", tags=["appointments"])

CALENDAR_TOKEN_EXPIRED_MSG = (
    "Your Google Calendar connection has expired. Please go to the doctor dashboard and reconnect "
    "Google Calendar to enable Meet links for appointments."
)


async def _clear_doctor_calendar_token_and_raise(db, doctor_id: str):
    """Clear expired Google Calendar token so UI shows reconnect prompt."""
    await db.user.update(
        where={"id": doctor_id},
        data={"googleRefreshToken": None},
    )
    raise HTTPException(status_code=400, detail=CALENDAR_TOKEN_EXPIRED_MSG)


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


def _serialize_appointment(
    a,
    include_patient=False,
    include_doctor=False,
    patient_has_assigned_forms=None,
    patient_has_pending_clinical_intake=None,
    patient_has_scheduled_followup=None,
):
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
    if patient_has_assigned_forms is not None:
        out["patientHasAssignedForms"] = patient_has_assigned_forms
    if patient_has_pending_clinical_intake is not None:
        out["patientHasPendingClinicalIntake"] = patient_has_pending_clinical_intake
    if patient_has_scheduled_followup is not None:
        out["patientHasScheduledFollowup"] = patient_has_scheduled_followup
    return out


class CreateAppointmentBody(BaseModel):
    scheduledAt: str
    durationMinutes: int = 60
    type: str = "followup"
    notes: str | None = None


class UpdateAppointmentBody(BaseModel):
    status: str | None = None
    notes: str | None = None


class SetAllowedFollowupBody(BaseModel):
    allowedTypes: list[str]  # ["followup_med_30", "followup_med_therapy_45"]


class ScheduleForPatientBody(BaseModel):
    patientId: str
    scheduledAt: str  # ISO datetime
    durationMinutes: int  # 30, 45, or 60 (for clinical_intake)
    type: str  # followup_med_30 | followup_med_therapy_45 | clinical_intake


class RejectAfterOrientationBody(BaseModel):
    rejectedNote: str | None = None


class RescheduleAppointmentBody(BaseModel):
    scheduledAt: str  # ISO datetime for new slot


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

    rejected_after_orientation = getattr(
        patient_user, "rejectedAfterOrientation", None
    ) or getattr(patient_user, "rejected_after_orientation", None)
    if rejected_after_orientation:
        rejected_after_orientation = True

    # If patient has pending_confirmation clinical intake, don't show clinical intake in book (they must confirm or cancel first)
    pending_clinical = await db.appointment.find_first(
        where={
            "patientId": patient_id,
            "type": "clinical_intake",
            "status": "pending_confirmation",
        },
    )
    has_pending_clinical_intake = pending_clinical is not None

    allowed = []
    if not has_orientation:
        allowed.append({
            "type": "orientation_consult",
            "durationMinutes": APPOINTMENT_TYPES["orientation_consult"]["durationMinutes"],
            "label": APPOINTMENT_TYPES["orientation_consult"]["label"],
        })
    if (
        has_orientation
        and is_approved
        and no_pending_forms
        and not has_intake
        and not rejected_after_orientation
        and not has_pending_clinical_intake
    ):
        allowed.append({
            "type": "clinical_intake",
            "durationMinutes": APPOINTMENT_TYPES["clinical_intake"]["durationMinutes"],
            "label": APPOINTMENT_TYPES["clinical_intake"]["label"],
        })
    if has_intake:
        # Only show follow-ups the doctor assigned after clinical intake
        allowed_followup = getattr(patient_user, "allowedFollowupTypes", None) or getattr(
            patient_user, "allowed_followup_types", None
        )
        followup_types = []
        if allowed_followup and isinstance(allowed_followup, list):
            followup_types = [t for t in allowed_followup if t in ("followup_med_30", "followup_med_therapy_45")]
        for apt_type in followup_types:
            if apt_type in APPOINTMENT_TYPES:
                allowed.append({
                    "type": apt_type,
                    "durationMinutes": APPOINTMENT_TYPES[apt_type]["durationMinutes"],
                    "label": APPOINTMENT_TYPES[apt_type]["label"],
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
        elif rejected_after_orientation:
            not_allowed_reasons["clinical_intake"] = "The practice has determined this service is not a fit at this time."
        elif has_pending_clinical_intake:
            not_allowed_reasons["clinical_intake"] = "You have a scheduled clinical intake. Confirm or change it in your appointments."
        else:
            not_allowed_reasons["clinical_intake"] = "Complete your initial evaluation and get approved first."
    if "followup_med_30" not in allowed_set:
        not_allowed_reasons["followup_med_30"] = (
            "Doctor will assign your follow-up type after clinical intake."
            if has_intake else "Complete clinical intake first."
        )
    if "followup_med_therapy_45" not in allowed_set:
        not_allowed_reasons["followup_med_therapy_45"] = (
            "Doctor will assign your follow-up type after clinical intake."
            if has_intake else "Complete clinical intake first."
        )

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
        where = {**where, "status": {"in": ["scheduled", "card_on_file", "paid", "pending_confirmation"]}}

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

    # For doctor: add orientation/intake action flags
    items = []
    for a in page:
        has_forms = None
        has_clinical = None
        has_scheduled_followup = None
        if getattr(a, "patient", None):
            patient_id = a.patient.id
            if a.type == "orientation_consult":
                has_forms = (
                    await db.patientformassignment.count(
                        where={"patientId": patient_id},
                    )
                ) > 0
                has_clinical = (
                    await db.appointment.count(
                        where={
                            "patientId": patient_id,
                            "type": "clinical_intake",
                            "status": {"in": ["pending_confirmation", "scheduled", "card_on_file", "paid"]},
                        },
                    )
                ) > 0
            if a.type in ("orientation_consult", "clinical_intake", "intake") and a.status == "completed":
                has_scheduled_followup = (
                    await db.appointment.count(
                        where={
                            "patientId": patient_id,
                            "type": {"in": ["followup_med_30", "followup_med_therapy_45"]},
                            "status": {"in": ["pending_confirmation", "scheduled", "card_on_file", "paid"]},
                        },
                    )
                ) > 0
        items.append(
            _serialize_appointment(
                a,
                include_patient=True,
                patient_has_assigned_forms=has_forms,
                patient_has_pending_clinical_intake=has_clinical,
                patient_has_scheduled_followup=has_scheduled_followup,
            )
        )
    return {
        "items": items,
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
        rejected = getattr(patient_user, "rejectedAfterOrientation", None) or getattr(patient_user, "rejected_after_orientation", None)
        if rejected:
            raise HTTPException(
                status_code=403,
                detail="The practice has determined this service is not a fit at this time.",
            )
    if body.type in ("followup_med_30", "followup_med_therapy_45"):
        if not has_intake:
            raise HTTPException(status_code=403, detail="Complete clinical intake before booking follow-ups.")
        allowed_followup = getattr(patient_user, "allowedFollowupTypes", None) or getattr(
            patient_user, "allowed_followup_types", None
        )
        if not allowed_followup or not isinstance(allowed_followup, list) or body.type not in allowed_followup:
            raise HTTPException(
                status_code=403,
                detail="Your doctor will assign your follow-up type after clinical intake.",
            )

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
    google_calendar_event_id = None
    refresh_token = getattr(doctor, "googleRefreshToken", None) or getattr(doctor, "google_refresh_token", None)
    calendar_id = getattr(doctor, "googleCalendarId", None) or getattr(doctor, "google_calendar_id", None) or "primary"
    if refresh_token:
        from app.google_calendar import GoogleCalendarTokenExpiredError, create_calendar_event_with_meet
        try:
            end_utc = scheduled_at + timedelta(minutes=body.durationMinutes or SLOT_MINUTES)
            meet_link, google_calendar_event_id = create_calendar_event_with_meet(
                refresh_token=refresh_token,
                calendar_id=calendar_id,
                start_utc=scheduled_at,
                end_utc=end_utc,
                summary=f"TelePsych — {patient_name}",
                description="Appointment booked via TelePsych.",
                attendee_email=patient_email,
            )
        except GoogleCalendarTokenExpiredError:
            await _clear_doctor_calendar_token_and_raise(db, doctor.id)

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
            "googleCalendarEventId": google_calendar_event_id,
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


@router.get("/patient/{patient_id}/allowed-followup-types")
async def get_patient_allowed_followup_types(
    patient_id: str,
    session: dict = Depends(require_doctor),
):
    """Get which follow-up types a patient can book (doctor only)."""
    db = await get_db()
    patient = await db.user.find_unique(where={"id": patient_id})
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")
    raw = getattr(patient, "allowedFollowupTypes", None) or getattr(patient, "allowed_followup_types", None)
    allowed = [t for t in (raw if isinstance(raw, list) else []) if t in ("followup_med_30", "followup_med_therapy_45")]
    return {"allowedTypes": allowed}


@router.put("/patient/{patient_id}/allowed-followup-types")
async def set_patient_allowed_followup_types(
    patient_id: str,
    body: SetAllowedFollowupBody,
    session: dict = Depends(require_doctor),
):
    """Set which follow-up types (30 or 45 min) a patient can book. Call after clinical intake."""
    db = await get_db()
    patient = await db.user.find_unique(where={"id": patient_id})
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")
    valid = [t for t in body.allowedTypes if t in ("followup_med_30", "followup_med_therapy_45")]
    await db.user.update(
        where={"id": patient_id},
        data={"allowedFollowupTypes": prisma_fields.Json(data=valid)},
    )
    return {"ok": True, "allowedTypes": valid}


@router.post("/{id}/reject-after-orientation")
async def reject_after_orientation(
    id: str,
    body: RejectAfterOrientationBody,
    session: dict = Depends(require_doctor),
):
    """Mark patient as rejected after orientation. Clinical intake will no longer be shown to them."""
    db = await get_db()
    appointment = await db.appointment.find_unique(
        where={"id": id},
        include={"patient": True},
    )
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    doctor_id = getattr(appointment, "doctorId", None) or getattr(appointment, "doctor_id", None)
    if doctor_id != session["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment")
    if getattr(appointment, "type", None) != "orientation_consult":
        raise HTTPException(
            status_code=400,
            detail="Can only reject after orientation for a completed orientation appointment.",
        )
    if getattr(appointment, "status", None) not in ("completed", "paid"):
        raise HTTPException(
            status_code=400,
            detail="Orientation must be completed before rejecting.",
        )
    patient_id = getattr(appointment, "patientId", None) or getattr(appointment, "patient_id", None)
    if not patient_id:
        raise HTTPException(status_code=400, detail="Appointment has no patient")
    await db.user.update(
        where={"id": patient_id},
        data={"rejectedAfterOrientation": True},
    )
    return {"ok": True, "message": "Patient will no longer see clinical intake option."}


@router.post("/schedule-for-patient")
async def schedule_for_patient(body: ScheduleForPatientBody, session: dict = Depends(require_doctor)):
    """Doctor schedules a follow-up or clinical intake for a patient. Creates appointment with status pending_confirmation.
    Patient will then add card and confirm."""
    db = await get_db()
    doctor = await db.user.find_first(where={"role": "doctor"})
    if not doctor or doctor.id != session["id"]:
        raise HTTPException(status_code=403, detail="Not a doctor")
    patient = await db.user.find_unique(where={"id": body.patientId})
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")

    if body.type == "clinical_intake":
        if body.durationMinutes != 60:
            raise HTTPException(status_code=400, detail="Clinical intake must be 60 minutes")
        completed = await db.appointment.find_many(
            where={
                "patientId": body.patientId,
                "status": {"in": ["completed", "paid"]},
            },
        )
        completed_types = {a.type for a in completed}
        if "orientation_consult" not in completed_types:
            raise HTTPException(
                status_code=400,
                detail="Patient must complete orientation before scheduling clinical intake.",
            )
        if "clinical_intake" in completed_types or "intake" in completed_types:
            raise HTTPException(
                status_code=400,
                detail="Patient has already completed clinical intake.",
            )
        rejected = getattr(patient, "rejectedAfterOrientation", None) or getattr(patient, "rejected_after_orientation", None)
        if rejected:
            raise HTTPException(
                status_code=400,
                detail="Patient was rejected after orientation.",
            )
        existing = await db.appointment.find_first(
            where={
                "patientId": body.patientId,
                "type": "clinical_intake",
                "status": {"in": ["pending_confirmation", "scheduled", "card_on_file"]},
            },
        )
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Patient already has a pending or scheduled clinical intake.",
            )
    else:
        if body.type not in ("followup_med_30", "followup_med_therapy_45"):
            raise HTTPException(status_code=400, detail="Type must be followup_med_30, followup_med_therapy_45, or clinical_intake")
        raw = getattr(patient, "allowedFollowupTypes", None) or getattr(patient, "allowed_followup_types", None)
        allowed_list = [t for t in (raw if isinstance(raw, list) else []) if t in ("followup_med_30", "followup_med_therapy_45")]
        # If no types set, allow both (doctor can schedule any follow-up after clinical intake)
        if allowed_list and body.type not in allowed_list:
            raise HTTPException(
                status_code=400,
                detail="Patient is not assigned this follow-up type. Set allowed follow-up types first.",
            )
        if body.durationMinutes not in (30, 45):
            raise HTTPException(status_code=400, detail="durationMinutes must be 30 or 45")

    try:
        scheduled_at = datetime.fromisoformat(body.scheduledAt.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid scheduledAt (use ISO format)")
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    apt = await db.appointment.create(
        data={
            "patientId": body.patientId,
            "doctorId": doctor.id,
            "scheduledAt": scheduled_at,
            "durationMinutes": body.durationMinutes,
            "type": body.type,
            "status": "pending_confirmation",
        },
    )
    return {"ok": True, "appointment": _serialize_appointment(apt, include_patient=True)}


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

    # When cancelling, delete the Google Calendar event if we have it
    if body.status == "cancelled":
        event_id = getattr(appointment, "googleCalendarEventId", None) or getattr(
            appointment, "google_calendar_event_id", None
        )
        if event_id:
            doctor = await db.user.find_unique(
                where={"id": (getattr(appointment, "doctorId", None) or getattr(appointment, "doctor_id", None))}
            )
            if doctor:
                refresh_token = getattr(doctor, "googleRefreshToken", None) or getattr(doctor, "google_refresh_token", None)
                calendar_id = getattr(doctor, "googleCalendarId", None) or getattr(doctor, "google_calendar_id", None) or "primary"
                if refresh_token:
                    from app.google_calendar import GoogleCalendarTokenExpiredError, delete_calendar_event
                    try:
                        delete_calendar_event(
                            refresh_token=refresh_token,
                            calendar_id=calendar_id,
                            event_id=event_id,
                        )
                    except GoogleCalendarTokenExpiredError:
                        await _clear_doctor_calendar_token_and_raise(db, doctor.id)
            data["googleCalendarEventId"] = None  # Clear so we don't try to delete again

    if not data:
        return _serialize_appointment(appointment)
    updated = await db.appointment.update(
        where={"id": id},
        data=data,
        include={"patient": True, "doctor": True},
    )
    return _serialize_appointment(updated, include_patient=True, include_doctor=True)


RESCHEDULE_MIN_HOURS_BEFORE = 48
RESCHEDULABLE_STATUSES = {"scheduled", "card_on_file", "paid"}


@router.post("/{id}/change-proposed-time")
async def change_proposed_time(
    id: str,
    body: RescheduleAppointmentBody,
    session: dict = Depends(require_patient_or_doctor),
):
    """
    Change the proposed time for a pending_confirmation appointment.
    Patient or doctor can pick a new slot before the patient confirms.
    No calendar/Meet/email - those are created on confirm.
    """
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

    status = getattr(appointment, "status", None)
    if status != "pending_confirmation":
        raise HTTPException(
            status_code=400,
            detail="Can only change proposed time for appointments pending confirmation.",
        )

    try:
        new_scheduled_at = datetime.fromisoformat(body.scheduledAt.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid scheduledAt (use ISO format)")
    if new_scheduled_at.tzinfo is None:
        new_scheduled_at = new_scheduled_at.replace(tzinfo=timezone.utc)

    new_scheduled_practice = new_scheduled_at.astimezone(PRACTICE_TZ)
    slot_minutes = new_scheduled_practice.hour * 60 + new_scheduled_practice.minute
    if slot_minutes % 15 != 0 or new_scheduled_practice.second != 0 or new_scheduled_practice.microsecond != 0:
        raise HTTPException(
            status_code=400,
            detail="scheduledAt must be a 15-minute slot start (e.g. 10:00, 10:15, 10:30)",
        )

    duration_minutes = getattr(appointment, "durationMinutes", None) or getattr(appointment, "duration_minutes", 60)
    doctor = await db.user.find_unique(where={"id": doctor_id})
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    now_practice = datetime.now(PRACTICE_TZ)
    if new_scheduled_practice < now_practice:
        raise HTTPException(status_code=400, detail="Cannot change to a time in the past")

    day = new_scheduled_practice.date()
    day_start = datetime.combine(day, time(0, 0), tzinfo=PRACTICE_TZ)
    day_end = datetime.combine(day, time(23, 59), tzinfo=PRACTICE_TZ)
    start_hhmm = new_scheduled_practice.strftime("%H:%M")

    offered = await db.doctorbookableslot.find_first(
        where={
            "doctorId": doctor_id,
            "date": {"gte": f"{day.isoformat()}T00:00:00.000Z", "lte": f"{day.isoformat()}T23:59:59.999Z"},
            "startTime": start_hhmm,
            "durationMinutes": duration_minutes,
        },
    )
    if not offered:
        raise HTTPException(
            status_code=409,
            detail="This time is no longer available. Please choose another time.",
        )

    existing_appointments = await db.appointment.find_many(
        where={
            "doctorId": doctor_id,
            "status": {"not": "cancelled"},
            "scheduledAt": {"gte": day_start, "lte": day_end},
        },
    )

    candidate_start = new_scheduled_practice
    candidate_end = candidate_start + timedelta(minutes=duration_minutes)

    for a in existing_appointments:
        if a.id == id:
            continue
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

    updated = await db.appointment.update(
        where={"id": id},
        data={"scheduledAt": new_scheduled_at},
        include={"patient": True, "doctor": True},
    )

    return _serialize_appointment(updated, include_patient=True, include_doctor=True)


@router.post("/{id}/reschedule")
async def reschedule_appointment(
    id: str,
    body: RescheduleAppointmentBody,
    session: dict = Depends(require_patient_or_doctor),
):
    """
    Reschedule an appointment to a new time.
    Allowed until 48 hours before the appointment. Patient and doctor can both reschedule.
    """
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

    status = getattr(appointment, "status", None)
    if status not in RESCHEDULABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reschedule appointment with status '{status}'.",
        )

    scheduled_at = getattr(appointment, "scheduledAt", None) or getattr(appointment, "scheduled_at", None)
    if not scheduled_at:
        raise HTTPException(status_code=400, detail="Appointment has no scheduled time")
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    hours_until = (scheduled_at - now).total_seconds() / 3600
    if hours_until < RESCHEDULE_MIN_HOURS_BEFORE:
        raise HTTPException(
            status_code=400,
            detail="Reschedule is only allowed until 48 hours before the appointment.",
        )

    try:
        new_scheduled_at = datetime.fromisoformat(body.scheduledAt.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid scheduledAt (use ISO format)")
    if new_scheduled_at.tzinfo is None:
        new_scheduled_at = new_scheduled_at.replace(tzinfo=timezone.utc)

    new_scheduled_practice = new_scheduled_at.astimezone(PRACTICE_TZ)
    slot_minutes = new_scheduled_practice.hour * 60 + new_scheduled_practice.minute
    if slot_minutes % 15 != 0 or new_scheduled_practice.second != 0 or new_scheduled_practice.microsecond != 0:
        raise HTTPException(
            status_code=400,
            detail="scheduledAt must be a 15-minute slot start (e.g. 10:00, 10:15, 10:30)",
        )

    duration_minutes = getattr(appointment, "durationMinutes", None) or getattr(appointment, "duration_minutes", 60)
    apt_type = getattr(appointment, "type", None) or "orientation_consult"

    doctor = await db.user.find_unique(where={"id": doctor_id})
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    now_practice = datetime.now(PRACTICE_TZ)
    if new_scheduled_practice < now_practice:
        raise HTTPException(status_code=400, detail="Cannot reschedule to a time in the past")

    day = new_scheduled_practice.date()
    day_start = datetime.combine(day, time(0, 0), tzinfo=PRACTICE_TZ)
    day_end = datetime.combine(day, time(23, 59), tzinfo=PRACTICE_TZ)
    start_hhmm = new_scheduled_practice.strftime("%H:%M")

    offered = await db.doctorbookableslot.find_first(
        where={
            "doctorId": doctor_id,
            "date": {"gte": f"{day.isoformat()}T00:00:00.000Z", "lte": f"{day.isoformat()}T23:59:59.999Z"},
            "startTime": start_hhmm,
            "durationMinutes": duration_minutes,
        },
    )
    if not offered:
        raise HTTPException(
            status_code=409,
            detail="This time is no longer available. Please choose another time.",
        )

    existing_appointments = await db.appointment.find_many(
        where={
            "doctorId": doctor_id,
            "status": {"not": "cancelled"},
            "scheduledAt": {"gte": day_start, "lte": day_end},
        },
    )

    candidate_start = new_scheduled_practice
    candidate_end = candidate_start + timedelta(minutes=duration_minutes)

    for a in existing_appointments:
        if a.id == id:
            continue
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

    meet_link = getattr(appointment, "meetLink", None) or getattr(appointment, "meet_link", None)
    google_calendar_event_id = getattr(appointment, "googleCalendarEventId", None) or getattr(
        appointment, "google_calendar_event_id", None
    )
    refresh_token = getattr(doctor, "googleRefreshToken", None) or getattr(doctor, "google_refresh_token", None)
    calendar_id = getattr(doctor, "googleCalendarId", None) or getattr(doctor, "google_calendar_id", None) or "primary"

    if refresh_token and getattr(appointment, "patient", None):
        patient_user = appointment.patient
        patient_name = (
            f"{_get_attr(patient_user, 'firstName', 'first_name', default='')} {_get_attr(patient_user, 'lastName', 'last_name', default='')}".strip()
            or "Patient"
        )
        patient_email = getattr(patient_user, "email", None)
        end_utc = new_scheduled_at + timedelta(minutes=duration_minutes)

        if google_calendar_event_id:
            # Update existing calendar event (preserves Meet link)
            from app.google_calendar import GoogleCalendarTokenExpiredError, update_calendar_event
            try:
                updated_meet_link = update_calendar_event(
                    refresh_token=refresh_token,
                    calendar_id=calendar_id,
                    event_id=google_calendar_event_id,
                    start_utc=new_scheduled_at,
                    end_utc=end_utc,
                    existing_meet_link=meet_link,
                )
                if updated_meet_link is not None:
                    meet_link = updated_meet_link
            except GoogleCalendarTokenExpiredError:
                await _clear_doctor_calendar_token_and_raise(db, doctor_id)
        else:
            # No event ID: create new calendar event (e.g. legacy or doctor-scheduled before confirm)
            from app.google_calendar import GoogleCalendarTokenExpiredError, create_calendar_event_with_meet
            try:
                meet_link, google_calendar_event_id = create_calendar_event_with_meet(
                    refresh_token=refresh_token,
                    calendar_id=calendar_id,
                    start_utc=new_scheduled_at,
                    end_utc=end_utc,
                    summary=f"TelePsych — {patient_name}",
                    description="Appointment rescheduled via TelePsych.",
                    attendee_email=patient_email,
                )
            except GoogleCalendarTokenExpiredError:
                await _clear_doctor_calendar_token_and_raise(db, doctor_id)

    update_data: dict = {"scheduledAt": new_scheduled_at}
    if meet_link is not None:
        update_data["meetLink"] = meet_link
    if google_calendar_event_id is not None:
        update_data["googleCalendarEventId"] = google_calendar_event_id

    updated = await db.appointment.update(
        where={"id": id},
        data=update_data,
        include={"patient": True, "doctor": True},
    )

    # Send reschedule email to patient with new date/time and Google Meet link
    patient_user = getattr(updated, "patient", None)
    if patient_user:
        from app.email_service import send_appointment_rescheduled_email
        patient_name = (
            f"{_get_attr(patient_user, 'firstName', 'first_name', default='')} {_get_attr(patient_user, 'lastName', 'last_name', default='')}".strip()
            or "Patient"
        )
        final_meet_link = getattr(updated, "meetLink", None) or getattr(updated, "meet_link", None) or meet_link
        send_appointment_rescheduled_email(
            to=patient_user.email,
            patient_first_name=patient_name,
            scheduled_at_iso=new_scheduled_at.isoformat(),
            meet_link=final_meet_link,
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
    amount_cents = get_visit_price_cents(appointment_type)
    if not price_id:
        # Fallback to amount_cents when Price IDs not configured (use env VISIT_PRICE_*_CENTS)
        price_id = None
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


@router.post("/{id}/confirm-by-patient")
async def confirm_by_patient(id: str, session: dict = Depends(require_patient)):
    """Patient confirms a doctor-scheduled appointment (adds card if needed, then confirms).
    Expects payment method to be attached via /api/payments/confirm-card before calling this.
    Updates status from pending_confirmation to card_on_file.
    Creates Google Meet link and sends confirmation email with appointment details."""
    db = await get_db()
    appointment = await db.appointment.find_unique(
        where={"id": id},
        include={"patient": True, "doctor": True},
    )
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    patient_id = getattr(appointment, "patientId", None) or getattr(appointment, "patient_id", None)
    if patient_id != session["id"]:
        raise HTTPException(status_code=403, detail="Not your appointment")
    if appointment.status != "pending_confirmation":
        raise HTTPException(
            status_code=400,
            detail=f"Appointment is not pending confirmation (status: {appointment.status})",
        )
    patient = await db.user.find_unique(where={"id": patient_id})
    stripe_cid = getattr(patient, "stripeCustomerId", None) or getattr(patient, "stripe_customer_id", None)
    if not patient or not stripe_cid:
        raise HTTPException(
            status_code=400,
            detail="Save your card first before confirming.",
        )
    # For clinical intake, patient must complete assigned forms before confirming
    apt_type = getattr(appointment, "type", None) or ""
    if apt_type == "clinical_intake":
        pending_forms = await db.patientformassignment.count(
            where={
                "patientId": patient_id,
                "status": "pending",
            },
        )
        if pending_forms > 0:
            raise HTTPException(
                status_code=403,
                detail="Complete your assigned forms before confirming. Visit Forms & Documents to complete them.",
            )

    # Create Google Meet link (when doctor has Calendar connected)
    meet_link = None
    google_calendar_event_id = None
    doctor = getattr(appointment, "doctor", None)
    patient_user = getattr(appointment, "patient", None)
    scheduled_at = getattr(appointment, "scheduledAt", None) or getattr(appointment, "scheduled_at", None)
    duration_min = getattr(appointment, "durationMinutes", None) or getattr(appointment, "duration_minutes", 60)

    if doctor and scheduled_at:
        refresh_token = getattr(doctor, "googleRefreshToken", None) or getattr(doctor, "google_refresh_token", None)
        calendar_id = getattr(doctor, "googleCalendarId", None) or getattr(doctor, "google_calendar_id", None) or "primary"
        patient_email = getattr(patient_user, "email", None) if patient_user else None
        patient_name = "Patient"
        if patient_user:
            patient_name = (
                f"{getattr(patient_user, 'firstName', None) or getattr(patient_user, 'first_name', '')} "
                f"{getattr(patient_user, 'lastName', None) or getattr(patient_user, 'last_name', '')}"
            ).strip() or "Patient"

        if refresh_token:
            from app.google_calendar import GoogleCalendarTokenExpiredError, create_calendar_event_with_meet
            try:
                end_utc = scheduled_at + timedelta(minutes=duration_min)
                meet_link, google_calendar_event_id = create_calendar_event_with_meet(
                    refresh_token=refresh_token,
                    calendar_id=calendar_id,
                    start_utc=scheduled_at,
                    end_utc=end_utc,
                    summary=f"TelePsych — {patient_name}",
                    description="Appointment confirmed via TelePsych.",
                    attendee_email=patient_email,
                )
            except GoogleCalendarTokenExpiredError:
                await _clear_doctor_calendar_token_and_raise(db, doctor.id)

    # Update appointment with status, Meet link, and calendar event ID
    await db.appointment.update(
        where={"id": id},
        data={
            "status": "card_on_file",
            "meetLink": meet_link,
            "googleCalendarEventId": google_calendar_event_id,
        },
    )

    # Send confirmation email with appointment details and Meet link
    if patient_user and scheduled_at:
        from app.email_service import send_appointment_confirmation_email
        send_appointment_confirmation_email(
            to=patient_user.email,
            patient_first_name=patient_name,
            scheduled_at_iso=scheduled_at.isoformat(),
            meet_link=meet_link,
        )

    return {"ok": True, "status": "card_on_file", "message": "Appointment confirmed. Your card is on file."}

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import get_db
from app.dependencies import require_patient, require_doctor, require_patient_or_doctor

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


def _get_attr(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _serialize_appointment(a, include_patient=False, include_doctor=False):
    scheduled_at = getattr(a, "scheduledAt", None) or getattr(a, "scheduled_at", None)
    duration = getattr(a, "duration_minutes", None) or getattr(a, "durationMinutes", 60)
    out = {
        "id": a.id,
        "scheduledAt": scheduled_at.isoformat() if scheduled_at else None,
        "durationMinutes": duration or 60,
        "status": a.status,
        "type": a.type,
        "notes": a.notes,
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


@router.get("/")
async def list_appointments(session: dict = Depends(require_patient_or_doctor)):
    db = await get_db()
    role = session["role"]
    if role == "patient":
        appointments = await db.appointment.find_many(
            where={"patientId": session["id"]},
            order={"scheduledAt": "desc"},
            include={"doctor": True},
        )
        return [_serialize_appointment(a, include_doctor=True) for a in appointments]
    else:
        appointments = await db.appointment.find_many(
            where={"doctorId": session["id"]},
            order={"scheduledAt": "desc"},
            include={"patient": True},
        )
        return [_serialize_appointment(a, include_patient=True) for a in appointments]


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
    doctor = await db.user.find_first(where={"role": "doctor"})
    if not doctor:
        raise HTTPException(status_code=503, detail="No provider available")
    appointment = await db.appointment.create(
        data={
            "patientId": session["id"],
            "doctorId": doctor.id,
            "scheduledAt": scheduled_at,
            "durationMinutes": body.durationMinutes,
            "type": body.type,
            "notes": body.notes,
            "status": "scheduled",
        },
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

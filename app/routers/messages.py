from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.dependencies import require_patient_or_doctor
from app.message_crypto import decrypt_message_body, encrypt_message_body

router = APIRouter(prefix="/api/messages", tags=["messages"])


def _get_attr(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


class StartThreadBody(BaseModel):
    userId: str


class SendMessageBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


def _thread_access_allowed(thread, session: dict) -> bool:
    role = session.get("role")
    user_id = session.get("id")
    patient_id = _get_attr(thread, "patientId", "patient_id")
    doctor_id = _get_attr(thread, "doctorId", "doctor_id")
    if role == "patient":
        return patient_id == user_id
    if role == "doctor":
        return doctor_id == user_id
    return False


@router.get("/summary")
async def get_messages_summary(session: dict = Depends(require_patient_or_doctor)):
    db = await get_db()
    role = session["role"]
    user_id = session["id"]
    where = {"patientId": user_id} if role == "patient" else {"doctorId": user_id}
    threads = await db.messagethread.find_many(where=where)
    thread_ids = [t.id for t in threads]
    if not thread_ids:
        return {"unreadCount": 0, "threadCount": 0}
    unread = await db.message.count(
        where={
            "threadId": {"in": thread_ids},
            "senderId": {"not": user_id},
            "readAt": None,
        }
    )
    return {"unreadCount": unread, "threadCount": len(thread_ids)}


@router.get("")
@router.get("/")
async def list_threads(session: dict = Depends(require_patient_or_doctor)):
    db = await get_db()
    role = session["role"]
    user_id = session["id"]
    where = {"patientId": user_id} if role == "patient" else {"doctorId": user_id}
    threads = await db.messagethread.find_many(
        where=where,
        order={"updatedAt": "desc"},
        include={"patient": True, "doctor": True},
    )

    out = []
    for t in threads:
        patient = getattr(t, "patient", None)
        doctor = getattr(t, "doctor", None)

        # Guard against bad legacy data: patient view must only pair with doctors,
        # doctor view must only pair with patients.
        if role == "patient":
            if not doctor or getattr(doctor, "role", None) != "doctor":
                continue
        else:
            if not patient or getattr(patient, "role", None) != "patient":
                continue

        unread_count = await db.message.count(
            where={"threadId": t.id, "senderId": {"not": user_id}, "readAt": None}
        )
        last_msg = await db.message.find_first(
            where={"threadId": t.id},
            order={"createdAt": "desc"},
            include={"sender": True},
        )
        counterparty = doctor if role == "patient" else patient
        out.append(
            {
                "id": t.id,
                "patientId": _get_attr(t, "patientId", "patient_id"),
                "doctorId": _get_attr(t, "doctorId", "doctor_id"),
                "updatedAt": (
                    _get_attr(t, "updatedAt", "updated_at").isoformat()
                    if _get_attr(t, "updatedAt", "updated_at")
                    else None
                ),
                "counterparty": {
                    "id": getattr(counterparty, "id", None),
                    "name": (
                        f"{_get_attr(counterparty, 'firstName', 'first_name', default='')} {_get_attr(counterparty, 'lastName', 'last_name', default='')}"
                    ).strip()
                    if counterparty
                    else "",
                    "email": getattr(counterparty, "email", None) if counterparty else None,
                    "role": getattr(counterparty, "role", None) if counterparty else None,
                },
                "unreadCount": unread_count,
                "lastMessage": {
                    "id": getattr(last_msg, "id", None),
                    "body": decrypt_message_body(getattr(last_msg, "body", None)),
                    "createdAt": (
                        _get_attr(last_msg, "createdAt", "created_at").isoformat()
                        if last_msg and _get_attr(last_msg, "createdAt", "created_at")
                        else None
                    ),
                    "senderId": _get_attr(last_msg, "senderId", "sender_id") if last_msg else None,
                }
                if last_msg
                else None,
            }
        )
    return {"threads": out}


@router.get("/contacts")
async def list_message_contacts(session: dict = Depends(require_patient_or_doctor)):
    db = await get_db()
    role = session["role"]
    user_id = session["id"]

    contacts_by_id: dict[str, dict] = {}
    if role == "patient":
        appts = await db.appointment.find_many(
            where={"patientId": user_id},
            include={"doctor": True},
            order={"createdAt": "desc"},
        )
        for a in appts:
            doctor = getattr(a, "doctor", None)
            if not doctor:
                continue
            if getattr(doctor, "role", None) != "doctor":
                continue
            contacts_by_id[doctor.id] = {
                "id": doctor.id,
                "name": f"{_get_attr(doctor, 'firstName', 'first_name', default='')} {_get_attr(doctor, 'lastName', 'last_name', default='')}".strip(),
                "email": getattr(doctor, "email", None),
                "role": "doctor",
            }
    else:
        patients = await db.user.find_many(
            where={"role": "patient"},
        )
        for patient in patients:
            if patient.id == user_id:
                continue
            if getattr(patient, "role", None) != "patient":
                continue
            contacts_by_id[patient.id] = {
                "id": patient.id,
                "name": f"{_get_attr(patient, 'firstName', 'first_name', default='')} {_get_attr(patient, 'lastName', 'last_name', default='')}".strip(),
                "email": getattr(patient, "email", None),
                "role": "patient",
            }

    contacts = list(contacts_by_id.values())
    return {"contacts": contacts}


@router.post("/thread-with-user")
async def get_or_create_thread_with_user(
    body: StartThreadBody,
    session: dict = Depends(require_patient_or_doctor),
):
    db = await get_db()
    me = await db.user.find_unique(where={"id": session["id"]})
    other = await db.user.find_unique(where={"id": body.userId})
    if not me or not other:
        raise HTTPException(status_code=404, detail="User not found")
    if me.id == other.id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")
    if me.role == other.role:
        raise HTTPException(status_code=403, detail="Messaging is only allowed between patient and doctor")

    patient_id = me.id if me.role == "patient" else other.id
    doctor_id = me.id if me.role == "doctor" else other.id
    thread = await db.messagethread.find_first(
        where={"patientId": patient_id, "doctorId": doctor_id}
    )
    if not thread:
        thread = await db.messagethread.create(
            data={
                "patient": {"connect": {"id": patient_id}},
                "doctor": {"connect": {"id": doctor_id}},
            }
        )
    return {"threadId": thread.id}


@router.get("/threads/{thread_id}")
async def get_thread_messages(
    thread_id: str,
    session: dict = Depends(require_patient_or_doctor),
):
    db = await get_db()
    thread = await db.messagethread.find_unique(
        where={"id": thread_id},
        include={"patient": True, "doctor": True},
    )
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if not _thread_access_allowed(thread, session):
        raise HTTPException(status_code=403, detail="Not your thread")

    now = datetime.now(timezone.utc)
    await db.message.update_many(
        where={"threadId": thread_id, "senderId": {"not": session["id"]}, "readAt": None},
        data={"readAt": now},
    )
    messages = await db.message.find_many(
        where={"threadId": thread_id},
        order={"createdAt": "asc"},
        include={"sender": True},
    )
    return {
        "thread": {
            "id": thread.id,
            "patientId": _get_attr(thread, "patientId", "patient_id"),
            "doctorId": _get_attr(thread, "doctorId", "doctor_id"),
            "patientName": (
                f"{_get_attr(thread.patient, 'firstName', 'first_name', default='')} {_get_attr(thread.patient, 'lastName', 'last_name', default='')}".strip()
                if getattr(thread, "patient", None)
                else ""
            ),
            "doctorName": (
                f"{_get_attr(thread.doctor, 'firstName', 'first_name', default='')} {_get_attr(thread.doctor, 'lastName', 'last_name', default='')}".strip()
                if getattr(thread, "doctor", None)
                else ""
            ),
        },
        "messages": [
            {
                "id": m.id,
                "body": decrypt_message_body(m.body),
                "senderId": _get_attr(m, "senderId", "sender_id"),
                "senderName": (
                    f"{_get_attr(m.sender, 'firstName', 'first_name', default='')} {_get_attr(m.sender, 'lastName', 'last_name', default='')}".strip()
                    if getattr(m, "sender", None)
                    else ""
                ),
                "createdAt": (
                    _get_attr(m, "createdAt", "created_at").isoformat()
                    if _get_attr(m, "createdAt", "created_at")
                    else None
                ),
                "readAt": (
                    _get_attr(m, "readAt", "read_at").isoformat()
                    if _get_attr(m, "readAt", "read_at")
                    else None
                ),
            }
            for m in messages
        ],
    }


@router.post("/threads/{thread_id}")
async def send_message(
    thread_id: str,
    body: SendMessageBody,
    session: dict = Depends(require_patient_or_doctor),
):
    db = await get_db()
    thread = await db.messagethread.find_unique(where={"id": thread_id})
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if not _thread_access_allowed(thread, session):
        raise HTTPException(status_code=403, detail="Not your thread")

    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    msg = await db.message.create(
        data={
            "thread": {"connect": {"id": thread_id}},
            "sender": {"connect": {"id": session["id"]}},
            "body": encrypt_message_body(text),
        },
        include={"sender": True},
    )
    await db.messagethread.update(
        where={"id": thread_id},
        data={"updatedAt": datetime.now(timezone.utc)},
    )
    return {
        "message": {
            "id": msg.id,
            "body": decrypt_message_body(msg.body),
            "senderId": _get_attr(msg, "senderId", "sender_id"),
            "senderName": (
                f"{_get_attr(msg.sender, 'firstName', 'first_name', default='')} {_get_attr(msg.sender, 'lastName', 'last_name', default='')}".strip()
                if getattr(msg, "sender", None)
                else ""
            ),
            "createdAt": (
                _get_attr(msg, "createdAt", "created_at").isoformat()
                if _get_attr(msg, "createdAt", "created_at")
                else None
            ),
        }
    }

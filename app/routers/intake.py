from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import get_db
from app.dependencies import require_patient, require_doctor

router = APIRouter(prefix="/api/intake", tags=["intake"])


def _get_attr(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


class IntakeFormBody(BaseModel):
    formData: dict


class SubmitIntakeBody(BaseModel):
    formData: dict


@router.get("/")
async def get_my_intake(session: dict = Depends(require_patient)):
    db = await get_db()
    sub = await db.intakesubmission.find_unique(
        where={"userId": session["id"]},
    )
    if not sub:
        return None
    form_data = _get_attr(sub, "formData", "form_data")
    reviewed_at = _get_attr(sub, "reviewedAt", "reviewed_at")
    created_at = _get_attr(sub, "createdAt", "created_at")
    updated_at = _get_attr(sub, "updatedAt", "updated_at")
    return {
        "id": sub.id,
        "formData": form_data,
        "status": sub.status,
        "reviewedAt": reviewed_at.isoformat() if reviewed_at else None,
        "createdAt": created_at.isoformat() if created_at else None,
        "updatedAt": updated_at.isoformat() if updated_at else None,
    }


@router.put("/")
async def upsert_intake(body: IntakeFormBody, session: dict = Depends(require_patient)):
    db = await get_db()
    form_data = body.formData
    sub = await db.intakesubmission.find_unique(where={"userId": session["id"]})
    if sub:
        if sub.status != "draft":
            raise HTTPException(
                status_code=400,
                detail="Intake already submitted; cannot edit.",
            )
        await db.intakesubmission.update(
            where={"id": sub.id},
            data={"formData": form_data},
        )
        return {"success": True, "status": "draft"}
    await db.intakesubmission.create(
        data={
            "userId": session["id"],
            "formData": form_data,
            "status": "draft",
        },
    )
    return {"success": True, "status": "draft"}


@router.post("/submit")
async def submit_intake(body: SubmitIntakeBody, session: dict = Depends(require_patient)):
    db = await get_db()
    sub = await db.intakesubmission.find_unique(where={"userId": session["id"]})
    if not sub:
        raise HTTPException(status_code=404, detail="Save a draft first.")
    if sub.status != "draft":
        raise HTTPException(status_code=400, detail="Intake already submitted.")
    await db.intakesubmission.update(
        where={"id": sub.id},
        data={"formData": body.formData, "status": "submitted"},
    )
    return {"success": True, "status": "submitted"}


# Doctor: list all intake submissions (must be before /{id})
@router.get("/all")
async def list_all_intakes(session: dict = Depends(require_doctor)):
    db = await get_db()
    subs = await db.intakesubmission.find_many(
        order={"updatedAt": "desc"},
        include={"user": True},
    )
    out = []
    for sub in subs:
        u = sub.user
        reviewed_at = _get_attr(sub, "reviewedAt", "reviewed_at")
        updated_at = _get_attr(sub, "updatedAt", "updated_at")
        out.append({
            "id": sub.id,
            "userId": _get_attr(sub, "userId", "user_id"),
            "patientName": f"{_get_attr(u, 'first_name', 'firstName', default='')} {_get_attr(u, 'last_name', 'lastName', default='')}".strip(),
            "patientEmail": u.email,
            "status": sub.status,
            "reviewedAt": reviewed_at.isoformat() if reviewed_at else None,
            "updatedAt": updated_at.isoformat() if updated_at else None,
        })
    return out


# Doctor: get one intake (full form data)
@router.get("/{id}")
async def get_intake_by_id(id: str, session: dict = Depends(require_doctor)):
    db = await get_db()
    sub = await db.intakesubmission.find_unique(
        where={"id": id},
        include={"user": True},
    )
    if not sub:
        raise HTTPException(status_code=404, detail="Intake not found")
    u = sub.user
    reviewed_at = _get_attr(sub, "reviewedAt", "reviewed_at")
    return {
        "id": sub.id,
        "userId": _get_attr(sub, "userId", "user_id"),
        "patientName": f"{_get_attr(u, 'first_name', 'firstName', default='')} {_get_attr(u, 'last_name', 'lastName', default='')}".strip(),
        "patientEmail": u.email,
        "formData": _get_attr(sub, "formData", "form_data"),
        "status": sub.status,
        "reviewedAt": reviewed_at.isoformat() if reviewed_at else None,
        "reviewedBy": _get_attr(sub, "reviewedBy", "reviewed_by"),
    }


# Doctor: mark as reviewed
@router.post("/{id}/review")
async def mark_reviewed(id: str, session: dict = Depends(require_doctor)):
    db = await get_db()
    sub = await db.intakesubmission.find_unique(where={"id": id})
    if not sub:
        raise HTTPException(status_code=404, detail="Intake not found")
    from datetime import datetime, timezone
    await db.intakesubmission.update(
        where={"id": id},
        data={
            "status": "reviewed",
            "reviewedAt": datetime.now(timezone.utc),
            "reviewedBy": session["id"],
        },
    )
    return {"success": True, "status": "reviewed"}

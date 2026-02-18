import os
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from prisma import fields as prisma_fields

from app.auth import generate_reset_token
from app.db import get_db
from app.dependencies import require_doctor
from app.email_service import send_acceptance_message_email, send_approval_email, send_rejection_email
from app.healthcare_fhir import delete_questionnaire_response, get_questionnaire_response_as_form_data

router = APIRouter(prefix="/api/patient-requests", tags=["patient-requests"])


class RejectBody(BaseModel):
    rejectedNote: str | None = None


def _get_attr(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


@router.get("/")
async def list_requests(session: dict = Depends(require_doctor)):
    db = await get_db()
    # Prisma Python: model PatientRequest -> client uses PatientRequest (PascalCase) or patient_request
    requests = await db.patientrequest.find_many(
        order={"createdAt": "desc"},
        include={"user": True},
    )
    out = []
    for r in requests:
        u = getattr(r, "user", None)
        consent_at = getattr(r, "consentAt", None) or getattr(r, "consent_at", None)
        questionnaire_data = getattr(r, "questionnaire_data", None)
        fhir_id = getattr(r, "fhirQuestionnaireResponseId", None) or getattr(r, "fhir_questionnaire_response_id", None)
        if fhir_id:
            from_fhir = get_questionnaire_response_as_form_data(fhir_id)
            questionnaire_data = from_fhir if from_fhir is not None else questionnaire_data
        created_at_val = _get_attr(r, "createdAt", "created_at")
        reviewed_by = _get_attr(r, "reviewedBy", "reviewed_by")
        auto_rejected = r.status == "rejected" and not reviewed_by
        out.append({
            "id": r.id,
            "firstName": _get_attr(r, "first_name", "firstName", default=""),
            "lastName": _get_attr(r, "last_name", "lastName", default=""),
            "email": r.email,
            "phone": r.phone,
            "notes": r.notes,
            "status": r.status,
            "rejectedNote": _get_attr(r, "rejected_note", "rejectedNote"),
            "autoRejected": auto_rejected,
            "createdAt": created_at_val.isoformat() if created_at_val else None,
            "consentAt": consent_at.isoformat() if consent_at else None,
            "questionnaireData": questionnaire_data,
            "user": {"id": u.id, "email": u.email} if u else None,
        })
    return out


@router.post("/{id}/approve")
async def approve(id: str, session: dict = Depends(require_doctor)):
    db = await get_db()
    req_record = await db.patientrequest.find_unique(        where={"id": id},
        include={"user": True},
    )
    if not req_record:
        raise HTTPException(status_code=404, detail="Request not found")
    # Allow approve for pending or rejected (doctor can override auto-reject)
    if req_record.status not in ("pending", "rejected"):
        raise HTTPException(status_code=400, detail="Request already processed")
    existing = await db.user.find_first(
        where={"email": req_record.email.lower(), "role": "patient"},
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="A patient account with this email already exists",
        )
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    reset_token = generate_reset_token()
    first_name = _get_attr(req_record, "first_name", "firstName", default="")
    last_name = _get_attr(req_record, "last_name", "lastName", default="")
    # Prisma Python data: use schema field names (camelCase) or Python (snake_case) per client
    user = await db.user.create(
        data={
            "email": req_record.email.lower(),
            "firstName": first_name,
            "lastName": last_name,
            "phone": req_record.phone,
            "role": "patient",
            "mustResetPassword": True,
            "passwordResetToken": reset_token,
            "passwordResetTokenExpiresAt": expires_at,
        },
    )
    await db.patientrequest.update(
        where={"id": id},
        data={
            "status": "approved",
            "reviewedAt": datetime.now(timezone.utc),
            "reviewedBy": session["id"],
            "userId": user.id,
        },
    )
    # Send emails via Resend (acceptance + approval with credentials)
    import logging
    _log = logging.getLogger(__name__)
    _log.info("Sending approval emails to %s (acceptance + credentials)", req_record.email)
    acc_ok, acc_err = send_acceptance_message_email(to=req_record.email, first_name=first_name)
    if not acc_ok:
        import logging
        logging.getLogger(__name__).warning("Acceptance message email failed: %s", acc_err)
    # 2) Account creation / password setup email (required for login)
    ok, err = send_approval_email(
        to=req_record.email,
        first_name=first_name,
        reset_token=reset_token,
    )
    if not ok:
        import logging
        logging.getLogger(__name__).error("Approval email failed: %s", err)
        # In production, block approval if email fails. In dev, allow so you can test without verified domain.
        env = os.environ.get("ENV", "").lower()
        is_production = env in ("production", "prod") or os.environ.get("FRONTEND_ORIGIN", "").strip().lower().startswith("https://")
        if is_production:
            raise HTTPException(
                status_code=500,
                detail="User approved but email failed: " + (err or "unknown"),
            )
        # Dev: user is already created; share credentials manually or fix Resend domain verification
        _log.warning("Approval succeeded but email was not sent (non-production). Share login details manually if needed.")
    return {"success": True, "userId": user.id}


@router.post("/{id}/reject")
async def reject(id: str, body: RejectBody, session: dict = Depends(require_doctor)):
    db = await get_db()
    req_record = await db.patientrequest.find_unique(where={"id": id})
    if not req_record:
        raise HTTPException(status_code=404, detail="Request not found")
    if req_record.status != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")
    await db.patientrequest.update(
        where={"id": id},
        data={
            "status": "rejected",
            "rejectedNote": body.rejectedNote,
            "reviewedAt": datetime.now(timezone.utc),
            "reviewedBy": session["id"],
        },
    )
    first_name = _get_attr(req_record, "first_name", "firstName", default="")
    rej_ok, rej_err = send_rejection_email(
        to=req_record.email,
        first_name=first_name,
        rejected_note=body.rejectedNote,
    )
    if not rej_ok:
        import logging
        logging.getLogger(__name__).warning("Rejection email failed: %s", rej_err)
    return {"success": True}


@router.delete("/{id}/questionnaire")
async def delete_questionnaire(id: str, session: dict = Depends(require_doctor)):
    """Delete the FHIR QuestionnaireResponse for this request (if any) and clear questionnaire data from DB."""
    db = await get_db()
    req_record = await db.patientrequest.find_unique(where={"id": id})
    if not req_record:
        raise HTTPException(status_code=404, detail="Request not found")
    fhir_id = getattr(req_record, "fhirQuestionnaireResponseId", None) or getattr(
        req_record, "fhir_questionnaire_response_id", None
    )
    if fhir_id:
        if not delete_questionnaire_response(fhir_id):
            raise HTTPException(
                status_code=502,
                detail="Failed to delete questionnaire record from storage. Please try again.",
            )
    await db.patientrequest.update(
        where={"id": id},
        data={
            "fhirQuestionnaireResponseId": None,
            "questionnaireData": prisma_fields.Json({}),  # Json type required for Prisma Json? field
        },
    )
    return {"success": True}

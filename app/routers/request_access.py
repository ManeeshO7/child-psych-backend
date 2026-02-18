from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_db
from app.email_service import send_rejection_email
from app.healthcare_fhir import create_questionnaire_response

router = APIRouter(prefix="/api/request-access", tags=["request-access"])

# Keys that trigger auto-reject when patient answers "yes"
AUTO_REJECT_KEYS = [
    "activeSuicidalThoughts",
    "psychiatricHospitalization",
    "psychoticSymptoms",
    "severeAggression",
    "legalCustodyCourt",
    "childProtectiveServices",
]

AUTO_REJECT_NOTE = "Auto-rejected based on pre-screening questionnaire responses. A provider may override this decision if appropriate."


def _should_auto_reject(form_data: dict) -> bool:
    if not form_data or not isinstance(form_data, dict):
        return False
    for key in AUTO_REJECT_KEYS:
        val = form_data.get(key)
        if val is not None and str(val).strip().lower() == "yes":
            return True
    return False


class RequestAccessBody(BaseModel):
    firstName: str
    lastName: str
    email: str
    phone: str
    notes: str | None = None


class ConsentBody(BaseModel):
    requestId: str


class QuestionnaireBody(BaseModel):
    requestId: str
    formData: dict


@router.post("/")
async def request_access(body: RequestAccessBody):
    if not body.firstName or not body.lastName or not body.email or not body.phone:
        raise HTTPException(
            status_code=400,
            detail="First name, last name, email, and phone are required",
        )
    db = await get_db()
    email = body.email.strip().lower()

    # Block if user has already completed the full request flow (questionnaire submitted)
    # — regardless of status: approved, rejected, or pending
    existing_completed = await db.patientrequest.find_first(
        where={
            "email": email,
            "fhirQuestionnaireResponseId": {"not": None},
        },
    )
    if existing_completed:
        status = getattr(existing_completed, "status", None) or "pending"
        if status == "approved":
            detail = "You already have access. Please log in to your account."
        elif status == "rejected":
            detail = "A previous request with this email was not approved. Please contact us if you have questions."
        else:
            detail = "You have already submitted a request with this email. We will email you with next steps."
        raise HTTPException(status_code=409, detail=detail)

    # Incomplete flow: pending, no questionnaire yet → update and continue
    existing = await db.patientrequest.find_first(
        where={"email": email, "status": "pending"},
    )
    if existing:
        await db.patientrequest.update(
            where={"id": existing.id},
            data={
                "firstName": body.firstName.strip(),
                "lastName": body.lastName.strip(),
                "phone": body.phone.strip(),
                "notes": body.notes.strip() if body.notes else None,
            },
        )
        return {"success": True, "requestId": existing.id}
    record = await db.patientrequest.create(
        data={
            "firstName": body.firstName.strip(),
            "lastName": body.lastName.strip(),
            "email": email,
            "phone": body.phone.strip(),
            "notes": body.notes.strip() if body.notes else None,
            "status": "pending",
        },
    )
    return {"success": True, "requestId": record.id}


@router.post("/consent")
async def record_consent(body: ConsentBody):
    if not (body.requestId or "").strip():
        raise HTTPException(status_code=400, detail="requestId is required")
    db = await get_db()
    record = await db.patientrequest.find_unique(where={"id": body.requestId.strip()})
    if not record:
        raise HTTPException(status_code=404, detail="Request not found")
    if record.status != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")
    await db.patientrequest.update(
        where={"id": record.id},
        data={"consentAt": datetime.now(timezone.utc)},
    )
    return {"success": True}


@router.post("/questionnaire")
async def submit_questionnaire(body: QuestionnaireBody):
    if not (body.requestId or "").strip():
        raise HTTPException(status_code=400, detail="requestId is required")
    if body.formData is None:
        raise HTTPException(status_code=400, detail="formData is required")
    db = await get_db()
    record = await db.patientrequest.find_unique(where={"id": body.requestId.strip()})
    if not record:
        raise HTTPException(status_code=404, detail="Request not found")
    consent_at = getattr(record, "consentAt", None) or getattr(record, "consent_at", None)
    if not consent_at:
        raise HTTPException(
            status_code=400,
            detail="Consent must be completed before submitting the questionnaire",
        )
    # Store questionnaire data in FHIR only (HIPAA-compliant); no fallback to app DB
    fhir_id = create_questionnaire_response(record.id, body.formData)
    if not fhir_id:
        raise HTTPException(
            status_code=503,
            detail="Questionnaire could not be saved at this time. Please try again later.",
        )
    first_name = getattr(record, "firstName", None) or getattr(record, "first_name", "")

    if _should_auto_reject(body.formData):
        await db.patientrequest.update(
            where={"id": record.id},
            data={
                "fhirQuestionnaireResponseId": fhir_id,
                "status": "rejected",
                "rejectedNote": AUTO_REJECT_NOTE,
                "reviewedAt": datetime.now(timezone.utc),
                # reviewedBy left None to indicate auto-reject (doctor can override)
            },
        )
        send_rejection_email(
            to=record.email,
            first_name=first_name,
            rejected_note=AUTO_REJECT_NOTE,
        )
        return {"success": True, "autoRejected": True}
    await db.patientrequest.update(
        where={"id": record.id},
        data={"fhirQuestionnaireResponseId": fhir_id},
    )
    return {"success": True}

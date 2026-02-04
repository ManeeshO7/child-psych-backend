from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_db
from app.healthcare_fhir import create_questionnaire_response

router = APIRouter(prefix="/api/request-access", tags=["request-access"])


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
    existing = await db.patientrequest.find_first(
        where={"email": email, "status": "pending"},
    )
    if existing:
        # Already completed questionnaire → block at request access
        fhir_id = getattr(existing, "fhirQuestionnaireResponseId", None) or getattr(
            existing, "fhir_questionnaire_response_id", None
        )
        if fhir_id:
            raise HTTPException(
                status_code=409,
                detail="You have already submitted a request with this email. We will contact you with next steps.",
            )
        # Incomplete flow (no questionnaire yet) → update and continue
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
    await db.patientrequest.update(
        where={"id": record.id},
        data={"fhirQuestionnaireResponseId": fhir_id},
    )
    return {"success": True}

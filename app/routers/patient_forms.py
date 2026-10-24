"""
Patient forms management endpoints.

- Doctor: List all forms, assign forms to patients, view patient overview
- Patient: View assigned forms, download PDF templates, upload PDFs, submit questionnaires
"""
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel

from app.db import get_db
from app.dependencies import require_doctor, require_patient
from app.gcs_service import (
    generate_signed_url,
    upload_patient_document,
    blob_exists,
)
from app.healthcare_fhir import (
    create_questionnaire_response,
    get_questionnaire_response_as_form_data,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/patient-forms", tags=["patient-forms"])

# GCS bucket names (from env or defaults)
TEMPLATES_BUCKET = os.environ.get("GCS_TEMPLATES_BUCKET", "telepsych-forms-templates")
PATIENT_DOCS_BUCKET = os.environ.get("GCS_PATIENT_DOCS_BUCKET", "telepsych-patient-documents")
USE_SIGNED_URLS_FOR_PATIENT_DOCS = os.environ.get("USE_SIGNED_URLS_FOR_PATIENT_DOCS", "false").strip().lower() in {"1", "true", "yes", "on"}
PATIENT_DOC_SIGNED_URL_TTL_MINUTES = int(os.environ.get("PATIENT_DOC_SIGNED_URL_TTL_MINUTES", "10"))


# ========== Pydantic Models ==========

class FormResponse(BaseModel):
    id: str
    title: str
    description: Optional[str]
    type: str  # "questionnaire" | "pdf_upload"
    questionnaireKey: Optional[str]
    templateGcsPath: Optional[str]
    isActive: bool


class PatientFormAssignmentResponse(BaseModel):
    id: str
    formId: str
    form: FormResponse
    status: str  # "pending" | "completed"
    assignedAt: str
    completedAt: Optional[str]
    downloadUrl: Optional[str] = None  # For PDF templates
    uploadUrl: Optional[str] = None  # For PDF uploads (signed PUT URL)


class AssignFormsBody(BaseModel):
    patientId: str
    formIds: list[str]
    appointmentId: Optional[str] = None


class SubmitQuestionnaireBody(BaseModel):
    assignmentId: str
    responses: dict  # Form data as key-value pairs


class PatientOverviewResponse(BaseModel):
    patientId: str
    patientName: str
    patientEmail: str
    appointments: list[dict]
    formAssignments: list[dict]


# ========== Doctor Endpoints ==========

@router.get("/patients")
async def list_all_patients(
    session: dict = Depends(require_doctor),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Items per page"),
    search: Optional[str] = Query(None, description="Filter by patient name"),
):
    """List patients (users with role=patient) with pagination and optional name search."""
    db = await get_db()
    where: dict = {"role": "patient"}
    if search and search.strip():
        q = search.strip()
        where = {
            "role": "patient",
            "OR": [
                {"firstName": {"contains": q, "mode": "insensitive"}},
                {"lastName": {"contains": q, "mode": "insensitive"}},
            ],
        }
    skip = (page - 1) * limit
    total = await db.user.count(where=where)
    users = await db.user.find_many(
        where=where,
        include={
            "appointmentsAsPatient": True,
        },
        order={"createdAt": "desc"},
        skip=skip,
        take=limit,
    )
    result = []
    for u in users:
        first_name = getattr(u, "firstName", None) or getattr(u, "first_name", "")
        last_name = getattr(u, "lastName", None) or getattr(u, "last_name", "")
        name = f"{first_name} {last_name}".strip() or "—"
        appointments = getattr(u, "appointmentsAsPatient", []) or []
        result.append({
            "id": u.id,
            "email": u.email,
            "name": name,
            "firstName": first_name,
            "lastName": last_name,
            "phone": getattr(u, "phone", None),
            "appointmentCount": len(appointments),
        })
    return {
        "patients": result,
        "total": total,
        "page": page,
        "limit": limit,
    }


def _serialize_form(f) -> dict:
    return {
        "id": f.id,
        "title": f.title,
        "description": getattr(f, "description", None),
        "type": f.type,
        "questionnaireKey": getattr(f, "questionnaireKey", None) or getattr(f, "questionnaire_key", None),
        "templateGcsPath": getattr(f, "templateGcsPath", None) or getattr(f, "template_gcs_path", None),
        "isActive": getattr(f, "isActive", True) if getattr(f, "isActive", None) is not None else getattr(f, "is_active", True),
    }


@router.get("/forms")
async def list_all_forms(
    includeInactive: bool = Query(False),
    session: dict = Depends(require_doctor),
):
    """List forms. By default only active forms (used by the assign-forms picker).
    Pass includeInactive=true for the doctor's form management view."""
    db = await get_db()
    where = {} if includeInactive else {"isActive": True}
    forms = await db.form.find_many(where=where, order={"createdAt": "desc"})
    return [_serialize_form(f) for f in forms]


class CreatePdfFormBody(BaseModel):
    title: str
    description: str | None = None


class UpdateFormBody(BaseModel):
    title: str | None = None
    description: str | None = None
    isActive: bool | None = None


@router.post("/forms/pdf")
async def create_pdf_form(
    title: str = Form(...),
    description: str | None = Form(None),
    file: UploadFile = File(...),
    session: dict = Depends(require_doctor),
):
    """Create a new PDF-upload form: doctor uploads a blank template PDF that
    patients will later download, fill out, and upload back completed."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="File is empty")

    db = await get_db()
    existing = await db.form.find_first(where={"title": title})
    if existing:
        raise HTTPException(status_code=400, detail="A form with this title already exists")

    form = await db.form.create(
        data={
            "title": title,
            "description": (description or "").strip() or None,
            "type": "pdf_upload",
        }
    )

    blob_name = f"forms/templates/{form.id}.pdf"
    uploaded_path = upload_patient_document(
        TEMPLATES_BUCKET,
        blob_name,
        content,
        content_type="application/pdf",
    )
    if not uploaded_path:
        # Clean up the form row so we don't leave a template-less pdf_upload form around.
        await db.form.delete(where={"id": form.id})
        raise HTTPException(status_code=500, detail="Failed to upload template PDF")

    form = await db.form.update(
        where={"id": form.id},
        data={"templateGcsPath": uploaded_path},
    )
    return _serialize_form(form)


@router.patch("/forms/{form_id}")
async def update_form(
    form_id: str,
    body: UpdateFormBody,
    session: dict = Depends(require_doctor),
):
    """Edit a form's title/description, or activate/deactivate it.
    Deactivating hides it from the assign-forms picker without deleting
    assignment/submission history (forms are never hard-deleted)."""
    db = await get_db()
    existing = await db.form.find_unique(where={"id": form_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Form not found")

    data: dict = {}
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        data["title"] = title
    if body.description is not None:
        data["description"] = body.description.strip() or None
    if body.isActive is not None:
        data["isActive"] = body.isActive

    if not data:
        return _serialize_form(existing)

    form = await db.form.update(where={"id": form_id}, data=data)
    return _serialize_form(form)


@router.post("/assign")
async def assign_forms_to_patient(
    body: AssignFormsBody,
    session: dict = Depends(require_doctor),
):
    """Assign forms to a patient (after intake appointment)."""
    db = await get_db()
    
    # Verify patient exists
    patient = await db.user.find_unique(where={"id": body.patientId})
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")
    
    # Verify forms exist
    forms = await db.form.find_many(where={"id": {"in": body.formIds}, "isActive": True})
    if len(forms) != len(body.formIds):
        raise HTTPException(status_code=400, detail="One or more forms not found")
    
    # Verify appointment if provided
    appointment = None
    if body.appointmentId:
        appointment = await db.appointment.find_unique(where={"id": body.appointmentId})
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")
    
    # Create assignments
    assignments = []
    for form in forms:
        assignment = await db.patientformassignment.create(
            data={
                "patientId": body.patientId,
                "formId": form.id,
                "appointmentId": body.appointmentId,
                "assignedBy": session["id"],
            }
        )
        assignments.append(assignment)
    
    return {"assigned": len(assignments), "assignmentIds": [a.id for a in assignments]}


@router.get("/patient/{patient_id}/overview")
async def get_patient_overview(
    patient_id: str,
    session: dict = Depends(require_doctor),
):
    """Get complete patient overview: appointments, form assignments, intake data."""
    db = await get_db()
    
    patient = await db.user.find_unique(
        where={"id": patient_id},
        include={
            "appointmentsAsPatient": {
                "orderBy": {"scheduledAt": "desc"},
            },
            "formAssignments": {
                "include": {
                    "form": True,
                    "submission": True,
                    "appointment": True,
                },
                "orderBy": {"assignedAt": "desc"},
            },
            "patientRequest": True,
            "patientProfile": True,
            "patientDocuments": {"orderBy": {"createdAt": "desc"}},
        },
    )
    
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")
    
    first_name = getattr(patient, "firstName", None) or getattr(patient, "first_name", "")
    last_name = getattr(patient, "lastName", None) or getattr(patient, "last_name", "")
    
    # Serialize appointments
    appointments = []
    for apt in getattr(patient, "appointmentsAsPatient", []):
        duration = getattr(apt, "durationMinutes", None) or getattr(apt, "duration_minutes", 30)
        appointments.append({
            "id": apt.id,
            "scheduledAt": getattr(apt, "scheduledAt", None) or getattr(apt, "scheduled_at", None),
            "status": apt.status,
            "type": apt.type,
            "notes": apt.notes,
            "meetLink": getattr(apt, "meetLink", None) or getattr(apt, "meet_link", None),
            "durationMinutes": int(duration) if duration is not None else 30,
        })
    
    # Serialize form assignments
    form_assignments = []
    for fa in getattr(patient, "formAssignments", []):
        form = getattr(fa, "form", None)
        submission = getattr(fa, "submission", None)
        
        fhir_id = getattr(fa, "fhirResponseId", None) or getattr(fa, "fhir_response_id", None)
        apt = getattr(fa, "appointment", None)
        apt_id = getattr(fa, "appointmentId", None) or getattr(fa, "appointment_id", None)
        assignment_data = {
            "id": fa.id,
            "formId": fa.formId if hasattr(fa, "formId") else getattr(fa, "form_id", ""),
            "formTitle": form.title if form else "Unknown",
            "formType": form.type if form else "unknown",
            "formQuestionnaireKey": getattr(form, "questionnaireKey", None) or getattr(form, "questionnaire_key", None) if form else None,
            "status": fa.status,
            "assignedAt": getattr(fa, "assignedAt", None) or getattr(fa, "assigned_at", None),
            "completedAt": getattr(fa, "completedAt", None) or getattr(fa, "completed_at", None),
            "fhirResponseId": fhir_id,
            "uploadedGcsPath": getattr(fa, "uploadedGcsPath", None) or getattr(fa, "uploaded_gcs_path", None),
            "appointmentId": apt_id,
        }
        if apt:
            sched = getattr(apt, "scheduledAt", None) or getattr(apt, "scheduled_at", None)
            assignment_data["appointmentScheduledAt"] = sched.isoformat() if sched else None
            assignment_data["appointmentType"] = getattr(apt, "type", None) or "unknown"
        
        # For questionnaire forms: fetch Q&A from FHIR for display (instead of raw response ID)
        if form and form.type == "questionnaire" and fhir_id:
            qa_data = get_questionnaire_response_as_form_data(fhir_id)
            if qa_data:
                assignment_data["questionnaireData"] = qa_data
        
        # Add download URL for PDF uploads
        if form and form.type == "pdf_upload" and assignment_data["uploadedGcsPath"]:
            signed_url = None
            if USE_SIGNED_URLS_FOR_PATIENT_DOCS:
                signed_url = generate_signed_url(
                    PATIENT_DOCS_BUCKET,
                    assignment_data["uploadedGcsPath"],
                    expiration_minutes=PATIENT_DOC_SIGNED_URL_TTL_MINUTES,
                )
            assignment_data["downloadUrl"] = signed_url
        
        form_assignments.append(assignment_data)
    
    # Serialize intake submission
    intake = getattr(patient, "intakeSubmission", None)
    intake_data = None
    if intake:
        intake_data = {
            "id": intake.id,
            "formData": getattr(intake, "formData", None) or getattr(intake, "form_data", None),
            "status": intake.status,
            "reviewedAt": getattr(intake, "reviewedAt", None) or getattr(intake, "reviewed_at", None),
        }
    
    # Pre-screening questionnaire (PatientRequest)
    patient_request = getattr(patient, "patientRequest", None)
    patient_request_data = None
    if patient_request:
        qdata = getattr(patient_request, "questionnaireData", None) or getattr(patient_request, "questionnaire_data", None)
        # Check if data is stored in FHIR and fetch it if available
        fhir_id = getattr(patient_request, "fhirQuestionnaireResponseId", None) or getattr(patient_request, "fhir_questionnaire_response_id", None)
        if fhir_id:
            from_fhir = get_questionnaire_response_as_form_data(fhir_id)
            if from_fhir is not None:
                qdata = from_fhir  # Use FHIR data if available
        
        patient_request_data = {
            "id": patient_request.id,
            "status": patient_request.status,
            "questionnaireData": qdata,
            "createdAt": getattr(patient_request, "createdAt", None) or getattr(patient_request, "created_at", None),
        }
    
    # Address from PatientProfile, pre-screening, or intake
    address = None
    patient_profile_data = None
    profile = getattr(patient, "patientProfile", None)
    if profile:
        addr = getattr(profile, "address", None)
        if addr:
            address = addr
        patient_profile_data = {
            "sex": getattr(profile, "sex", None),
            "dateOfBirth": None,
            "ssn": getattr(profile, "ssn", None),
            "address": getattr(profile, "address", None),
            "preferredPharmacyName": getattr(profile, "preferredPharmacyName", None) or getattr(profile, "preferred_pharmacy_name", None),
            "preferredPharmacyPhone": getattr(profile, "preferredPharmacyPhone", None) or getattr(profile, "preferred_pharmacy_phone", None),
            "preferredPharmacyAddress": getattr(profile, "preferredPharmacyAddress", None) or getattr(profile, "preferred_pharmacy_address", None),
            "guardian1Name": getattr(profile, "guardian1Name", None) or getattr(profile, "guardian_1_name", None),
            "guardian1Relationship": getattr(profile, "guardian1Relationship", None) or getattr(profile, "guardian_1_relationship", None),
            "guardian1Phone": getattr(profile, "guardian1Phone", None) or getattr(profile, "guardian_1_phone", None),
            "guardian2Name": getattr(profile, "guardian2Name", None) or getattr(profile, "guardian_2_name", None),
            "guardian2Relationship": getattr(profile, "guardian2Relationship", None) or getattr(profile, "guardian_2_relationship", None),
            "guardian2Phone": getattr(profile, "guardian2Phone", None) or getattr(profile, "guardian_2_phone", None),
        }
        dob = getattr(profile, "dateOfBirth", None) or getattr(profile, "date_of_birth", None)
        if dob:
            patient_profile_data["dateOfBirth"] = dob.isoformat()[:10] if hasattr(dob, "isoformat") else str(dob)[:10]
    if not address and patient_request_data and patient_request_data.get("questionnaireData"):
        qdata = patient_request_data["questionnaireData"]
        if isinstance(qdata, dict):
            address = qdata.get("childResidenceAddress") or qdata.get("childLocation") or qdata.get("address")

    # Serialize patient documents (profile uploads: previous medical records)
    patient_docs = []
    for d in getattr(patient, "patientDocuments", []) or []:
        gcs_path = getattr(d, "gcsPath", None) or getattr(d, "gcs_path", "")
        download_url = None
        if gcs_path and USE_SIGNED_URLS_FOR_PATIENT_DOCS:
            download_url = generate_signed_url(
                PATIENT_DOCS_BUCKET,
                gcs_path,
                expiration_minutes=PATIENT_DOC_SIGNED_URL_TTL_MINUTES,
            )
        fallback_url = f"/api/patient-documents/{d.id}/download" if gcs_path else None
        patient_docs.append({
            "id": d.id,
            "displayName": getattr(d, "displayName", None) or getattr(d, "display_name", ""),
            "fileName": getattr(d, "fileName", None) or getattr(d, "file_name", ""),
            "contentType": getattr(d, "contentType", None) or getattr(d, "content_type", "application/pdf"),
            "createdAt": getattr(d, "createdAt", None) or getattr(d, "created_at", None),
            "downloadUrl": download_url or fallback_url,
        })

    fhir_patient_id = getattr(patient, "fhirPatientId", None) or getattr(patient, "fhir_patient_id", None)

    return {
        "patientId": patient.id,
        "fhirPatientId": fhir_patient_id,
        "patientName": f"{first_name} {last_name}".strip(),
        "patientEmail": patient.email,
        "patientPhone": getattr(patient, "phone", None),
        "address": address or (patient_profile_data and patient_profile_data.get("address")),
        "patientProfile": patient_profile_data,
        "patientRequest": patient_request_data,
        "patientDocuments": patient_docs,
        "appointments": appointments,
        "formAssignments": form_assignments,
        "intakeSubmission": intake_data,
    }


# ========== Patient Endpoints ==========

@router.get("/my-forms")
async def get_my_assigned_forms(session: dict = Depends(require_patient)):
    """Get all forms assigned to the current patient."""
    db = await get_db()
    
    assignments = await db.patientformassignment.find_many(
        where={"patientId": session["id"]},
        include={"form": True},
        order={"assignedAt": "desc"},
    )
    
    result = []
    for fa in assignments:
        form = getattr(fa, "form", None)
        if not form:
            continue
        
        form_type = form.type
        download_url = None
        upload_url = None
        
        # For PDF templates: generate download URL
        if form_type == "pdf_upload":
            template_path = getattr(form, "templateGcsPath", None) or getattr(form, "template_gcs_path", None)
            if template_path:
                download_url = generate_signed_url(
                    TEMPLATES_BUCKET,
                    template_path,
                    expiration_minutes=60,
                )
            
            # Generate signed PUT URL for patient to upload completed PDF
            if fa.status == "pending":
                upload_blob_name = f"patient-{session['id']}/forms/{fa.id}.pdf"
                upload_url = generate_signed_url(
                    PATIENT_DOCS_BUCKET,
                    upload_blob_name,
                    expiration_minutes=60,
                    method="PUT",
                )
        
        result.append({
            "id": fa.id,
            "formId": fa.formId if hasattr(fa, "formId") else getattr(fa, "form_id", ""),
            "form": {
                "id": form.id,
                "title": form.title,
                "description": getattr(form, "description", None),
                "type": form_type,
                "questionnaireKey": getattr(form, "questionnaireKey", None) or getattr(form, "questionnaire_key", None),
            },
            "status": fa.status,
            "assignedAt": getattr(fa, "assignedAt", None) or getattr(fa, "assigned_at", None),
            "completedAt": getattr(fa, "completedAt", None) or getattr(fa, "completed_at", None),
            "downloadUrl": download_url,
            "uploadUrl": upload_url,
        })
    
    return result


@router.post("/submit-questionnaire")
async def submit_questionnaire(
    body: SubmitQuestionnaireBody,
    session: dict = Depends(require_patient),
):
    """Submit a questionnaire form (stores in FHIR)."""
    db = await get_db()
    
    assignment = await db.patientformassignment.find_unique(
        where={"id": body.assignmentId},
        include={"form": True},
    )
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    if assignment.patientId != session["id"]:
        raise HTTPException(status_code=403, detail="Not your assignment")
    
    if assignment.status == "completed":
        raise HTTPException(status_code=400, detail="Already completed")
    
    form = getattr(assignment, "form", None)
    if not form or form.type != "questionnaire":
        raise HTTPException(status_code=400, detail="Not a questionnaire form")
    
    # Store in FHIR
    fhir_response_id = create_questionnaire_response(
        patient_request_id=assignment.id,  # Use assignment ID as reference
        form_data=body.responses,
    )
    
    # Update assignment
    await db.patientformassignment.update(
        where={"id": body.assignmentId},
        data={
            "status": "completed",
            "completedAt": datetime.utcnow(),
            "fhirResponseId": fhir_response_id,
        },
    )
    
    # Create submission record
    await db.formsubmission.create(
        data={
            "assignmentId": body.assignmentId,
        },
    )
    
    return {"success": True, "fhirResponseId": fhir_response_id}


@router.post("/upload-pdf/{assignment_id}")
async def upload_pdf_form(
    assignment_id: str,
    file: UploadFile = File(...),
    session: dict = Depends(require_patient),
):
    """Upload a completed PDF form."""
    db = await get_db()
    
    assignment = await db.patientformassignment.find_unique(
        where={"id": assignment_id},
        include={"form": True},
    )
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    if assignment.patientId != session["id"]:
        raise HTTPException(status_code=403, detail="Not your assignment")
    
    if assignment.status == "completed":
        raise HTTPException(status_code=400, detail="Already completed")
    
    form = getattr(assignment, "form", None)
    if not form or form.type != "pdf_upload":
        raise HTTPException(status_code=400, detail="Not a PDF upload form")
    
    # Validate file type
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")
    
    # Read file content
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="File is empty")
    
    # Upload to GCS
    blob_name = f"patient-{session['id']}/forms/{assignment_id}.pdf"
    uploaded_path = upload_patient_document(
        PATIENT_DOCS_BUCKET,
        blob_name,
        content,
        content_type="application/pdf",
    )
    
    if not uploaded_path:
        raise HTTPException(status_code=500, detail="Failed to upload file")
    
    # Update assignment
    await db.patientformassignment.update(
        where={"id": assignment_id},
        data={
            "status": "completed",
            "completedAt": datetime.utcnow(),
            "uploadedGcsPath": uploaded_path,
        },
    )
    
    # Create submission record
    await db.formsubmission.create(
        data={
            "assignmentId": assignment_id,
        },
    )
    
    return {"success": True, "uploadedPath": uploaded_path}


@router.get("/check-pending")
async def check_pending_forms(
    appointmentId: str | None = None,
    session: dict = Depends(require_patient),
):
    """Check if patient has pending forms. If appointmentId given, only counts forms for that appointment (or unlinked)."""
    db = await get_db()
    where: dict = {
        "patientId": session["id"],
        "status": "pending",
    }
    if appointmentId:
        where["OR"] = [
            {"appointmentId": appointmentId},
            {"appointmentId": None},
        ]
    pending_count = await db.patientformassignment.count(where=where)
    return {"hasPendingForms": pending_count > 0, "pendingCount": pending_count}

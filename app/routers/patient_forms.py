"""
Patient forms management endpoints.

- Doctor: List all forms, assign forms to patients, view patient overview
- Patient: View assigned forms, download PDF templates, upload PDFs, submit questionnaires
"""
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from app.db import get_db
from app.dependencies import require_doctor, require_patient
from app.gcs_service import (
    generate_signed_url,
    upload_patient_document,
    blob_exists,
)
from app.healthcare_fhir import create_questionnaire_response, get_questionnaire_response_as_form_data

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/patient-forms", tags=["patient-forms"])

# GCS bucket names (from env or defaults)
TEMPLATES_BUCKET = os.environ.get("GCS_TEMPLATES_BUCKET", "telepsych-forms-templates")
PATIENT_DOCS_BUCKET = os.environ.get("GCS_PATIENT_DOCS_BUCKET", "telepsych-patient-documents")


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
    intakeSubmission: Optional[dict] = None


# ========== Doctor Endpoints ==========

@router.get("/patients")
async def list_all_patients(session: dict = Depends(require_doctor)):
    """List all patients (users with role=patient) for the doctor dashboard."""
    db = await get_db()
    users = await db.user.find_many(
        where={"role": "patient"},
        include={
            "appointmentsAsPatient": True,
            "intakeSubmission": True,
        },
        order={"createdAt": "desc"},
    )
    result = []
    for u in users:
        first_name = getattr(u, "firstName", None) or getattr(u, "first_name", "")
        last_name = getattr(u, "lastName", None) or getattr(u, "last_name", "")
        name = f"{first_name} {last_name}".strip() or "—"
        intake = getattr(u, "intakeSubmission", None)
        appointments = getattr(u, "appointmentsAsPatient", []) or []
        result.append({
            "id": u.id,
            "email": u.email,
            "name": name,
            "firstName": first_name,
            "lastName": last_name,
            "phone": getattr(u, "phone", None),
            "appointmentCount": len(appointments),
            "intakeStatus": intake.status if intake else None,
        })
    return result


@router.get("/forms")
async def list_all_forms(session: dict = Depends(require_doctor)):
    """List all available forms (active only)."""
    db = await get_db()
    forms = await db.form.find_many(where={"isActive": True}, order={"createdAt": "desc"})
    return [
        {
            "id": f.id,
            "title": f.title,
            "description": getattr(f, "description", None),
            "type": f.type,
            "questionnaireKey": getattr(f, "questionnaireKey", None) or getattr(f, "questionnaire_key", None),
            "templateGcsPath": getattr(f, "templateGcsPath", None) or getattr(f, "template_gcs_path", None),
            "isActive": getattr(f, "isActive", True) or getattr(f, "is_active", True),
        }
        for f in forms
    ]


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
                },
                "orderBy": {"assignedAt": "desc"},
            },
            "intakeSubmission": True,
            "patientRequest": True,
        },
    )
    
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")
    
    first_name = getattr(patient, "firstName", None) or getattr(patient, "first_name", "")
    last_name = getattr(patient, "lastName", None) or getattr(patient, "last_name", "")
    
    # Serialize appointments
    appointments = []
    for apt in getattr(patient, "appointmentsAsPatient", []):
        appointments.append({
            "id": apt.id,
            "scheduledAt": getattr(apt, "scheduledAt", None) or getattr(apt, "scheduled_at", None),
            "status": apt.status,
            "type": apt.type,
            "notes": apt.notes,
            "meetLink": getattr(apt, "meetLink", None) or getattr(apt, "meet_link", None),
        })
    
    # Serialize form assignments
    form_assignments = []
    for fa in getattr(patient, "formAssignments", []):
        form = getattr(fa, "form", None)
        submission = getattr(fa, "submission", None)
        
        assignment_data = {
            "id": fa.id,
            "formId": fa.formId if hasattr(fa, "formId") else getattr(fa, "form_id", ""),
            "formTitle": form.title if form else "Unknown",
            "formType": form.type if form else "unknown",
            "status": fa.status,
            "assignedAt": getattr(fa, "assignedAt", None) or getattr(fa, "assigned_at", None),
            "completedAt": getattr(fa, "completedAt", None) or getattr(fa, "completed_at", None),
            "fhirResponseId": getattr(fa, "fhirResponseId", None) or getattr(fa, "fhir_response_id", None),
            "uploadedGcsPath": getattr(fa, "uploadedGcsPath", None) or getattr(fa, "uploaded_gcs_path", None),
        }
        
        # Add download URL for PDF uploads
        if form and form.type == "pdf_upload" and assignment_data["uploadedGcsPath"]:
            signed_url = generate_signed_url(
                PATIENT_DOCS_BUCKET,
                assignment_data["uploadedGcsPath"],
                expiration_minutes=60,
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
    
    # Address from pre-screening (childResidenceAddress) or intake if needed
    address = None
    if patient_request_data and patient_request_data.get("questionnaireData"):
        qdata = patient_request_data["questionnaireData"]
        if isinstance(qdata, dict):
            address = qdata.get("childResidenceAddress") or qdata.get("childLocation") or qdata.get("address")
    if not address and intake_data and intake_data.get("formData"):
        form_data = intake_data["formData"]
        if isinstance(form_data, dict):
            address = form_data.get("address") or form_data.get("childResidenceAddress") or form_data.get("childLocation")
    
    return {
        "patientId": patient.id,
        "patientName": f"{first_name} {last_name}".strip(),
        "patientEmail": patient.email,
        "patientPhone": getattr(patient, "phone", None),
        "address": address,
        "patientRequest": patient_request_data,
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
async def check_pending_forms(session: dict = Depends(require_patient)):
    """Check if patient has pending forms (used before booking)."""
    db = await get_db()
    
    pending_count = await db.patientformassignment.count(
        where={
            "patientId": session["id"],
            "status": "pending",
        },
    )
    
    return {"hasPendingForms": pending_count > 0, "pendingCount": pending_count}

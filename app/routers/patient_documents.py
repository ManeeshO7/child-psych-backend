"""
Patient-uploaded documents (e.g. previous medical records) in profile.
Stored in GCS, accessible by patient and doctor.
"""
import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.db import get_db
from app.dependencies import require_patient
from app.gcs_service import generate_signed_url, upload_patient_document

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/patient-documents", tags=["patient-documents"])

PATIENT_DOCS_BUCKET = os.environ.get("GCS_PATIENT_DOCS_BUCKET", "telepsych-patient-documents")

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _get_extension(filename: str) -> str:
    i = filename.rfind(".")
    return filename[i:].lower() if i >= 0 else ""


def _content_type_for_ext(ext: str) -> str:
    m = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    return m.get(ext, "application/octet-stream")


@router.get("/")
async def list_my_documents(session: dict = Depends(require_patient)):
    """List all documents uploaded by the current patient."""
    db = await get_db()
    docs = await db.patientdocument.find_many(
        where={"userId": session["id"]},
        order={"createdAt": "desc"},
    )
    result = []
    for d in docs:
        download_url = generate_signed_url(
            PATIENT_DOCS_BUCKET,
            d.gcsPath if hasattr(d, "gcsPath") else getattr(d, "gcs_path", ""),
            expiration_minutes=60,
        )
        result.append({
            "id": d.id,
            "displayName": getattr(d, "displayName", None) or getattr(d, "display_name", ""),
            "fileName": getattr(d, "fileName", None) or getattr(d, "file_name", ""),
            "contentType": getattr(d, "contentType", None) or getattr(d, "content_type", "application/pdf"),
            "createdAt": getattr(d, "createdAt", None) or getattr(d, "created_at"),
            "downloadUrl": download_url,
        })
    return {"documents": result}


@router.post("/upload")
async def upload_document(
    session: dict = Depends(require_patient),
    file: UploadFile = File(...),
    displayName: str = Form(default="Previous medical records"),
):
    """Upload a document (PDF or image) for the patient profile."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = _get_extension(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Allowed types: PDF, PNG, JPG, GIF, WebP. Got: {ext or 'unknown'}",
        )
    content_type = file.content_type or _content_type_for_ext(ext)
    if content_type not in ALLOWED_CONTENT_TYPES:
        content_type = _content_type_for_ext(ext)

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="File is empty")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")

    db = await get_db()
    doc_id = str(uuid.uuid4())[:8]
    safe_name = "".join(c if c.isalnum() or c in "-." else "_" for c in file.filename)[:100]
    blob_name = f"patient-{session['id']}/profile-docs/{doc_id}_{safe_name}"

    uploaded_path = upload_patient_document(
        PATIENT_DOCS_BUCKET,
        blob_name,
        content,
        content_type=content_type,
    )
    if not uploaded_path:
        raise HTTPException(status_code=500, detail="Failed to upload file")

    doc = await db.patientdocument.create(
        data={
            "userId": session["id"],
            "displayName": (displayName or "Previous medical records").strip()[:200],
            "fileName": file.filename[:200],
            "gcsPath": uploaded_path,
            "contentType": content_type,
        },
    )
    return {
        "id": doc.id,
        "displayName": getattr(doc, "displayName", None) or getattr(doc, "display_name", ""),
        "fileName": getattr(doc, "fileName", None) or getattr(doc, "file_name", ""),
        "createdAt": getattr(doc, "createdAt", None) or getattr(doc, "created_at"),
    }

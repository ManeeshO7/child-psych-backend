"""
Google Cloud Storage service for patient form PDFs.

- Templates: Blank PDFs stored in forms-templates bucket
- Uploads: Patient-completed PDFs stored in patient-documents bucket
- Uses signed URLs for secure, time-limited access
"""
import logging
import os
from datetime import timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from google.cloud import storage
    GCS_AVAILABLE = True
except ImportError:
    GCS_AVAILABLE = False
    logger.warning("google-cloud-storage not installed. PDF form features will be disabled.")


def _get_gcs_client():
    """Get GCS client. Returns None if not configured."""
    if not GCS_AVAILABLE:
        return None
    try:
        return storage.Client()
    except Exception as e:
        logger.error(f"Failed to initialize GCS client: {e}")
        return None


def generate_signed_url(
    bucket_name: str,
    blob_name: str,
    expiration_minutes: int = 15,
    method: str = "GET",
) -> Optional[str]:
    """
    Generate a short-lived signed URL for downloading/viewing a GCS object.
    
    Args:
        bucket_name: GCS bucket name
        blob_name: Path within bucket (e.g. "forms/templates/Clinical_Workflow_Blueprint.pdf")
        expiration_minutes: URL validity duration (default 15 minutes)
        method: HTTP method ("GET" for download/view, "PUT" for upload)
    
    Returns:
        Signed URL string, or None if GCS is not available/configured
    """
    client = _get_gcs_client()
    if not client:
        return None
    
    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        
        url = blob.generate_signed_url(
            expiration=timedelta(minutes=expiration_minutes),
            method=method,
            version="v4",
        )
        return url
    except Exception as e:
        logger.error(f"Failed to generate signed URL for {bucket_name}/{blob_name}: {e}")
        return None


def upload_patient_document(
    bucket_name: str,
    blob_name: str,
    file_content: bytes,
    content_type: str = "application/pdf",
) -> Optional[str]:
    """
    Upload patient-uploaded PDF to GCS.
    
    Args:
        bucket_name: GCS bucket name (e.g. "telepsych-patient-documents")
        blob_name: Path within bucket (e.g. "patient-{id}/forms/{assignmentId}.pdf")
        file_content: PDF file bytes
        content_type: MIME type (default "application/pdf")
    
    Returns:
        GCS blob path if successful, None otherwise
    """
    client = _get_gcs_client()
    if not client:
        return None
    
    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(file_content, content_type=content_type)
        logger.info(f"Uploaded {blob_name} to {bucket_name}")
        return blob_name
    except Exception as e:
        logger.error(f"Failed to upload {blob_name} to {bucket_name}: {e}")
        return None


def download_patient_document(bucket_name: str, blob_name: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Download a blob from GCS and return content + content type.

    Returns:
        (bytes, content_type) on success, (None, None) on failure
    """
    client = _get_gcs_client()
    if not client:
        return None, None

    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        content = blob.download_as_bytes()
        return content, blob.content_type or "application/octet-stream"
    except Exception as e:
        logger.error(f"Failed to download {blob_name} from {bucket_name}: {e}")
        return None, None


def delete_blob(bucket_name: str, blob_name: str) -> bool:
    """Delete a blob from GCS. Returns True if successful."""
    client = _get_gcs_client()
    if not client:
        return False
    
    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.delete()
        logger.info(f"Deleted {blob_name} from {bucket_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete {blob_name} from {bucket_name}: {e}")
        return False


def blob_exists(bucket_name: str, blob_name: str) -> bool:
    """Check if a blob exists in GCS."""
    client = _get_gcs_client()
    if not client:
        return False
    
    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        return blob.exists()
    except Exception:
        return False

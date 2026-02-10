"""
Google Cloud Healthcare API (FHIR) integration for pre-screening questionnaire responses.
Stores PHI as FHIR QuestionnaireResponse resources; app DB holds only the resource ID reference.
When env vars are not set, FHIR is disabled and questionnaire data is stored in the app DB only.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Canonical URL for our pre-screening questionnaire (no actual Questionnaire resource required)
QUESTIONNAIRE_CANONICAL = "http://telepsych.example.com/Questionnaire/prescreening"


def _fhir_config() -> dict[str, str] | None:
    project_id = os.environ.get("GCP_PROJECT_ID", "").strip()
    location = os.environ.get("GCP_LOCATION", "").strip()
    dataset_id = os.environ.get("GCP_DATASET_ID", "").strip()
    fhir_store_id = os.environ.get("GCP_FHIR_STORE_ID", "").strip()
    if not all([project_id, location, dataset_id, fhir_store_id]):
        return None
    return {
        "project_id": project_id,
        "location": location,
        "dataset_id": dataset_id,
        "fhir_store_id": fhir_store_id,
    }


def _build_fhir_items(form_data: dict[str, Any]) -> list[dict]:
    """Convert our formData keys/values into FHIR QuestionnaireResponse.item[].answer[].value[x]."""
    items = []
    for link_id, value in form_data.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            items.append({"linkId": link_id, "answer": [{"valueBoolean": value}]})
        elif isinstance(value, str):
            items.append({"linkId": link_id, "answer": [{"valueString": value}]})
        elif isinstance(value, list):
            # e.g. primaryConcerns: ["ADHD...", "Anxiety..."]
            answers = [{"valueString": str(v)} for v in value if v is not None and v != ""]
            if answers:
                items.append({"linkId": link_id, "answer": answers})
        else:
            items.append({"linkId": link_id, "answer": [{"valueString": str(value)}]})
    return items


def _form_data_from_fhir_items(items: list[dict]) -> dict[str, Any]:
    """Convert FHIR QuestionnaireResponse.item[] back to our formData shape."""
    out: dict[str, Any] = {}
    for item in items or []:
        link_id = item.get("linkId")
        if not link_id:
            continue
        answers = item.get("answer") or []
        if not answers:
            continue
        first = answers[0]
        if "valueBoolean" in first:
            out[link_id] = first["valueBoolean"]
        elif "valueString" in first:
            if len(answers) == 1:
                out[link_id] = first["valueString"]
            else:
                out[link_id] = [a.get("valueString", "") for a in answers if "valueString" in a]
        else:
            out[link_id] = first.get("valueString", str(first))
    return out


def create_questionnaire_response(patient_request_id: str, form_data: dict) -> str | None:
    """
    Create a FHIR QuestionnaireResponse in Google Healthcare API and return its resource ID.
    Returns None if FHIR is not configured or creation fails.
    """
    config = _fhir_config()
    if not config:
        return None
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        parent = (
            f"projects/{config['project_id']}/locations/{config['location']}"
            f"/datasets/{config['dataset_id']}/fhirStores/{config['fhir_store_id']}"
        )
        items = _build_fhir_items(form_data)
        # FHIR R4: questionnaire is canonical (0..1), status required, subject 0..1.
        # Google may require subject; use identifier-based reference when no Patient exists.
        # Google Healthcare API expects QuestionnaireResponse.identifier as a single
        # Identifier object when present, not an array; omit it and use subject.identifier for linking.
        body = {
            "resourceType": "QuestionnaireResponse",
            "status": "completed",
            "questionnaire": QUESTIONNAIRE_CANONICAL,
            "subject": {
                "identifier": {
                    "system": "urn:telepsych:patient-request",
                    "value": patient_request_id,
                },
            },
            "authored": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "item": items,
        }
        request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .create(parent=parent, type="QuestionnaireResponse", body=body)
        )
        request.headers["content-type"] = "application/fhir+json;charset=utf-8"
        response = request.execute()
        resource_id = response.get("id")
        if resource_id:
            logger.info("Created FHIR QuestionnaireResponse %s for request %s", resource_id, patient_request_id)
            return resource_id
    except GoogleHttpError as e:
        # Log API error body to diagnose 400 Bad Request
        try:
            err_content = e.content.decode("utf-8") if isinstance(e.content, bytes) else str(e.content)
            logger.warning("FHIR create 400/error body: %s", err_content)
        except Exception:
            pass
        logger.exception("FHIR create failed: %s", e)
    except Exception as e:
        logger.exception("FHIR create failed: %s", e)
    return None


def get_questionnaire_response_as_form_data(resource_id: str) -> dict[str, Any] | None:
    """
    Read a FHIR QuestionnaireResponse from Google Healthcare API and return its contents
    as our formData shape (for doctor dashboard display). Returns None if not configured or read fails.
    """
    config = _fhir_config()
    if not config or not resource_id:
        return None
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        name = (
            f"projects/{config['project_id']}/locations/{config['location']}"
            f"/datasets/{config['dataset_id']}/fhirStores/{config['fhir_store_id']}"
            f"/fhir/QuestionnaireResponse/{resource_id}"
        )
        request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .read(name=name)
        )
        response = request.execute()
        items = response.get("item") or []
        return _form_data_from_fhir_items(items)
    except GoogleHttpError as e:
        if e.resp and e.resp.status == 404:
            logger.info("FHIR QuestionnaireResponse %s not found (deleted or never created)", resource_id)
        else:
            logger.warning("FHIR read failed for %s: %s", resource_id, e)
    except Exception as e:
        logger.warning("FHIR read failed for %s: %s", resource_id, e)
    return None


def delete_questionnaire_response(resource_id: str) -> bool:
    """
    Delete a FHIR QuestionnaireResponse from Google Healthcare API.
    Returns True if deleted, already gone (404), or FHIR not configured; False on other error.
    """
    config = _fhir_config()
    if not config or not resource_id:
        return True  # nothing to delete
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        name = (
            f"projects/{config['project_id']}/locations/{config['location']}"
            f"/datasets/{config['dataset_id']}/fhirStores/{config['fhir_store_id']}"
            f"/fhir/QuestionnaireResponse/{resource_id}"
        )
        (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .delete(name=name)
        ).execute()
        logger.info("Deleted FHIR QuestionnaireResponse %s", resource_id)
        return True
    except GoogleHttpError as e:
        if e.resp and e.resp.status == 404:
            logger.info("FHIR QuestionnaireResponse %s already gone (404)", resource_id)
            return True
        logger.warning("FHIR delete failed for %s: %s", resource_id, e)
    except Exception as e:
        logger.warning("FHIR delete failed for %s: %s", resource_id, e)
    return False

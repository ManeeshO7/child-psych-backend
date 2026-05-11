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


# --- Encounter (appointment notes) for HIPAA-compliant storage ---

def _fhir_encounter_parent(config: dict) -> str:
    # Historical name, but this is actually the generic FHIR store parent.
    return (
        f"projects/{config['project_id']}/locations/{config['location']}"
        f"/datasets/{config['dataset_id']}/fhirStores/{config['fhir_store_id']}"
    )


def create_encounter_with_note(
    appointment_id: str,
    patient_id: str,
    doctor_id: str,
    scheduled_at_iso: str,
    note_text: str,
) -> str | None:
    """
    Create a FHIR R4 Encounter with encounter note (Annotation) for HIPAA-compliant storage.
    Returns the Encounter resource ID, or None if FHIR is not configured or creation fails.
    """
    config = _fhir_config()
    if not config:
        return None
    if not note_text or not note_text.strip():
        return None
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        parent = _fhir_encounter_parent(config)
        # Period: use scheduled time as start; end = start + 1 hour for display
        start = scheduled_at_iso
        end = scheduled_at_iso
        try:
            from datetime import timedelta
            iso = scheduled_at_iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            end_dt = dt + timedelta(hours=1)
            end = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except Exception:
            pass

        body = {
            "resourceType": "Encounter",
            "status": "finished",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "AMB",
                "display": "ambulatory",
            },
            "identifier": [
                {"system": "urn:telepsych:appointment", "value": appointment_id},
            ],
            "subject": {
                "identifier": {"system": "urn:telepsych:patient", "value": patient_id},
            },
            "participant": [
                {
                    "type": [
                        {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationType",
                                    "code": "PPRF",
                                    "display": "primary performer",
                                }
                            ]
                        }
                    ],
                    "individual": {
                        "identifier": {"system": "urn:telepsych:practitioner", "value": doctor_id},
                    },
                }
            ],
            "period": {"start": start, "end": end},
            "note": [{"text": note_text.strip()}],
        }
        request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .create(parent=parent, type="Encounter", body=body)
        )
        request.headers["content-type"] = "application/fhir+json;charset=utf-8"
        response = request.execute()
        resource_id = response.get("id")
        if resource_id:
            logger.info("Created FHIR Encounter %s for appointment %s", resource_id, appointment_id)
            return resource_id
    except GoogleHttpError as e:
        try:
            err_content = e.content.decode("utf-8") if isinstance(e.content, bytes) else str(e.content)
            logger.warning("FHIR Encounter create error body: %s", err_content)
        except Exception:
            pass
        logger.exception("FHIR Encounter create failed: %s", e)
    except Exception as e:
        logger.exception("FHIR Encounter create failed: %s", e)
    return None


def update_encounter_note(fhir_encounter_id: str, note_text: str) -> bool:
    """
    Update the note on an existing FHIR Encounter. Returns True on success.
    """
    config = _fhir_config()
    if not config or not fhir_encounter_id:
        return False
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        name = (
            f"{_fhir_encounter_parent(config)}/fhir/Encounter/{fhir_encounter_id}"
        )
        request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .read(name=name)
        )
        body = request.execute()
        body["note"] = [{"text": (note_text or "").strip()}]
        update_request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .update(name=name, body=body)
        )
        update_request.headers["content-type"] = "application/fhir+json;charset=utf-8"
        update_request.execute()
        logger.info("Updated FHIR Encounter note %s", fhir_encounter_id)
        return True
    except GoogleHttpError as e:
        logger.warning("FHIR Encounter update failed for %s: %s", fhir_encounter_id, e)
    except Exception as e:
        logger.warning("FHIR Encounter update failed for %s: %s", fhir_encounter_id, e)
    return False


def get_encounter_note(fhir_encounter_id: str) -> str | None:
    """
    Read the encounter note text from a FHIR Encounter. Returns None if not found or not configured.
    """
    config = _fhir_config()
    if not config or not fhir_encounter_id:
        return None
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        name = (
            f"{_fhir_encounter_parent(config)}/fhir/Encounter/{fhir_encounter_id}"
        )
        request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .read(name=name)
        )
        body = request.execute()
        notes = body.get("note") or []
        if notes and isinstance(notes[0], dict):
            return notes[0].get("text") or None
        return None
    except GoogleHttpError as e:
        if e.resp and e.resp.status == 404:
            logger.info("FHIR Encounter %s not found", fhir_encounter_id)
        else:
            logger.warning("FHIR Encounter read failed for %s: %s", fhir_encounter_id, e)
    except Exception as e:
        logger.warning("FHIR Encounter read failed for %s: %s", fhir_encounter_id, e)
    return None


# --- Generic FHIR helpers ---


def get_patient_resource(patient_id: str) -> dict[str, Any] | None:
    """
    Read a Patient resource from the configured FHIR store.

    Returns the raw FHIR JSON dict, or None if not configured / not found.
    """
    config = _fhir_config()
    if not config or not patient_id:
        return None
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        name = (
            f"{_fhir_encounter_parent(config)}/fhir/Patient/{patient_id}"
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
        if response.get("resourceType") == "Patient":
            return response
        return None
    except GoogleHttpError as e:
        if e.resp and e.resp.status == 404:
            logger.info("FHIR Patient %s not found", patient_id)
        else:
            logger.warning("FHIR Patient read failed for %s: %s", patient_id, e)
    except Exception as e:
        logger.warning("FHIR Patient read failed for %s: %s", patient_id, e)
    return None


def create_fhir_resource(resource: dict[str, Any]) -> str | None:
    """
    Create a generic FHIR resource in the configured store.

    Returns the created resource ID on success, or None on failure.
    """
    config = _fhir_config()
    if not config:
        return None
    resource_type = resource.get("resourceType")
    if not resource_type:
        logger.warning("create_fhir_resource called without resourceType")
        return None
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        parent = _fhir_encounter_parent(config)
        request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .create(parent=parent, type=resource_type, body=resource)
        )
        request.headers["content-type"] = "application/fhir+json;charset=utf-8"
        response = request.execute()
        resource_id = response.get("id")
        if resource_id:
            logger.info("Created FHIR %s resource %s", resource_type, resource_id)
            return resource_id
    except GoogleHttpError as e:
        try:
            err_content = e.content.decode("utf-8") if isinstance(e.content, bytes) else str(e.content)
            logger.warning("FHIR create %s error body: %s", resource_type, err_content)
        except Exception:
            pass
        logger.exception("FHIR %s create failed: %s", resource_type, e)
    except Exception as e:
        logger.exception("FHIR %s create failed: %s", resource_type, e)
    return None


def update_fhir_resource(resource_type: str, resource_id: str, body: dict[str, Any]) -> bool:
    """
    Update an existing FHIR resource in the configured store.
    `body` must include resourceType and id and be the full resource.
    Returns True on success.
    """
    config = _fhir_config()
    if not config or not resource_id or not body:
        return False
    if body.get("resourceType") != resource_type or body.get("id") != resource_id:
        logger.warning("update_fhir_resource body resourceType/id mismatch")
        return False
    try:
        from googleapiclient.errors import HttpError as GoogleHttpError
        from googleapiclient import discovery

        client = discovery.build("healthcare", "v1")
        name = f"{_fhir_encounter_parent(config)}/fhir/{resource_type}/{resource_id}"
        request = (
            client.projects()
            .locations()
            .datasets()
            .fhirStores()
            .fhir()
            .update(name=name, body=body)
        )
        request.headers["content-type"] = "application/fhir+json;charset=utf-8"
        request.execute()
        logger.info("Updated FHIR %s/%s", resource_type, resource_id)
        return True
    except GoogleHttpError as e:
        logger.warning("FHIR %s update failed for %s: %s", resource_type, resource_id, e)
    except Exception as e:
        logger.warning("FHIR %s update failed for %s: %s", resource_type, resource_id, e)
    return False


# Portal user -> FHIR Patient link (identifier so we can resolve portal user to GCP Patient)
TELEPSYCH_PATIENT_IDENTIFIER_SYSTEM = "urn:telepsych:patient"


def _get_attr(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for n in names:
        v = getattr(obj, n, None)
        if v is not None and v != "":
            return v
    return default


def build_fhir_patient_payload(user: Any, profile: Any = None) -> dict[str, Any]:
    """
    Build a FHIR R4 Patient resource for create/update from portal User and optional PatientProfile.
    Includes identifier linking to portal user id (urn:telepsych:patient).
    """
    first = (_get_attr(user, "firstName", "first_name") or "").strip()
    last = (_get_attr(user, "lastName", "last_name") or "").strip()
    name = [{"use": "official", "family": last, "given": [first]}]
    if not first and not last:
        name = [{"use": "official", "given": ["Unknown"]}]

    payload: dict[str, Any] = {
        "resourceType": "Patient",
        "identifier": [
            {"system": TELEPSYCH_PATIENT_IDENTIFIER_SYSTEM, "value": (user.id if hasattr(user, "id") else user.get("id", ""))},
        ],
        "name": name,
    }

    telecom = []
    phone = _get_attr(user, "phone")
    if phone:
        telecom.append({"system": "phone", "value": str(phone).strip(), "use": "home"})
    email = _get_attr(user, "email")
    if email:
        telecom.append({"system": "email", "value": str(email).strip()})
    if telecom:
        payload["telecom"] = telecom

    if profile:
        gender = _get_attr(profile, "sex", "gender")
        if gender:
            payload["gender"] = str(gender).strip().replace("prefer_not_to_say", "unknown")
        dob = _get_attr(profile, "dateOfBirth", "date_of_birth")
        if dob:
            if hasattr(dob, "isoformat"):
                payload["birthDate"] = dob.isoformat()[:10]
            else:
                payload["birthDate"] = str(dob)[:10]
        address_str = _get_attr(profile, "address")
        if address_str:
            payload["address"] = [{"use": "home", "line": [str(address_str).strip()], "type": "physical"}]

    return payload


async def ensure_fhir_patient_for_user(db: Any, user_id: str) -> str | None:
    """
    Ensure a GCP FHIR Patient exists for the given portal user (patient).
    If the user already has fhirPatientId, return it. Otherwise create a Patient
    in the FHIR store from user + profile and persist the returned id on the user.
    Returns the GCP FHIR Patient.id or None if FHIR is disabled or creation fails.
    """
    if not user_id:
        return None
    user = await db.user.find_unique(
        where={"id": user_id},
        include={"patientProfile": True},
    )
    if not user or getattr(user, "role", None) != "patient":
        return None

    existing = _get_attr(user, "fhirPatientId", "fhir_patient_id")
    if existing:
        return existing

    profile = getattr(user, "patientProfile", None)
    payload = build_fhir_patient_payload(user, profile)
    created_id = create_fhir_resource(payload)
    if not created_id:
        return None

    await db.user.update(
        where={"id": user_id},
        data={"fhirPatientId": created_id},
    )
    logger.info("Linked User %s to FHIR Patient %s", user_id, created_id)
    return created_id


# --- Medications: search MedicationRequest / MedicationStatement from GCP FHIR ---


def _fhir_search(resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]:
    """
    Search for FHIR resources in the configured store via GET request.
    Returns a list of resource dicts (entry[].resource from the Bundle).
    """
    config = _fhir_config()
    if not config:
        return []
    try:
        import urllib.parse
        import google.auth
        from google.auth.transport.requests import Request as AuthRequest
        import httpx

        base = (
            f"https://healthcare.googleapis.com/v1/projects/{config['project_id']}"
            f"/locations/{config['location']}/datasets/{config['dataset_id']}"
            f"/fhirStores/{config['fhir_store_id']}/fhir"
        )
        query = urllib.parse.urlencode(params)
        url = f"{base}/{resource_type}?{query}" if query else f"{base}/{resource_type}"

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(AuthRequest())
        token = credentials.token

        resp = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/fhir+json",
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.warning("FHIR search %s failed: %s %s", resource_type, resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        if data.get("resourceType") != "Bundle" or data.get("type") != "searchset":
            return []
        entries = data.get("entry") or []
        return [e["resource"] for e in entries if isinstance(e.get("resource"), dict)]
    except Exception as e:
        logger.warning("FHIR search %s failed: %s", resource_type, e)
        return []


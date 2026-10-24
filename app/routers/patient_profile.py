"""
Patient profile (Name, Sex, DOB, SSN, Contact, Pharmacy, Guardians).
Required before booking orientation consultation.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.dependencies import require_patient
from app.healthcare_fhir import (
    build_fhir_patient_payload,
    ensure_fhir_patient_for_user,
    update_fhir_resource,
)

router = APIRouter(prefix="/api/patient-profile", tags=["patient-profile"])

# Any clinical_intake appointment that isn't cancelled counts as "scheduled"
# for the purpose of unlocking the preferred-pharmacy fields — including one
# still awaiting the patient's confirmation.
_SCHEDULED_APPOINTMENT_STATUSES = ["pending_confirmation", "scheduled", "card_on_file", "paid", "completed"]


async def _has_confirmed_clinical_intake(db, patient_id: str) -> bool:
    """Preferred pharmacy is only collected once the patient has a clinical
    intake appointment scheduled (any non-cancelled status)."""
    match = await db.appointment.find_first(
        where={
            "patientId": patient_id,
            "type": "clinical_intake",
            "status": {"in": _SCHEDULED_APPOINTMENT_STATUSES},
        },
    )
    return match is not None


class PatientProfileUpdateBody(BaseModel):
    firstName: Optional[str] = Field(None, min_length=1)
    lastName: Optional[str] = Field(None, min_length=1)
    preferredName: Optional[str] = None
    phone: Optional[str] = None
    sex: Optional[str] = None  # male | female | other | prefer_not_to_say
    race: Optional[str] = None
    preferredLanguage: Optional[str] = None
    dateOfBirth: Optional[str] = None  # YYYY-MM-DD
    ssn: Optional[str] = None
    address: Optional[str] = None
    preferredPharmacyName: Optional[str] = None
    preferredPharmacyPhone: Optional[str] = None
    preferredPharmacyAddress: Optional[str] = None
    guardian1Name: Optional[str] = None
    guardian1Relationship: Optional[str] = None
    guardian1Phone: Optional[str] = None
    guardian1Email: Optional[str] = None
    guardian1Address: Optional[str] = None
    guardian2Name: Optional[str] = None
    guardian2Relationship: Optional[str] = None
    guardian2Phone: Optional[str] = None
    guardian2Email: Optional[str] = None
    guardian2Address: Optional[str] = None


def _profile_is_complete(user, profile, intake_confirmed: bool = False) -> bool:
    """Check if profile has all required fields for orientation booking.

    Preferred pharmacy is only required once the patient has a confirmed
    clinical intake appointment; before that it isn't collected yet, so it
    must not block "profile complete" (which gates orientation booking,
    i.e. the step that happens before intake even exists)."""
    if not profile:
        return False
    # Required: name (on user), sex, dob, ssn, phone, address, parent 1;
    # preferred pharmacy only once intake_confirmed is True.
    first = (getattr(user, "firstName", None) or getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "lastName", None) or getattr(user, "last_name", "") or "").strip()
    phone = (getattr(user, "phone", None) or "").strip()
    sex = (getattr(profile, "sex", None) or "").strip()
    dob = getattr(profile, "dateOfBirth", None) or getattr(profile, "date_of_birth", None)
    ssn = (getattr(profile, "ssn", None) or "").strip()
    address = (getattr(profile, "address", None) or "").strip()
    g1_name = (getattr(profile, "guardian1Name", None) or getattr(profile, "guardian_1_name", None) or "").strip()
    g1_relationship = (getattr(profile, "guardian1Relationship", None) or getattr(profile, "guardian_1_relationship", None) or "").strip()
    g1_phone = (getattr(profile, "guardian1Phone", None) or getattr(profile, "guardian_1_phone", None) or "").strip()
    g1_address = (getattr(profile, "guardian1Address", None) or getattr(profile, "guardian_1_address", None) or "").strip()
    base_complete = bool(first and last and phone and sex and dob and ssn and address and g1_name and g1_relationship and g1_phone and g1_address)
    if not intake_confirmed:
        return base_complete
    pharmacy = (getattr(profile, "preferredPharmacyName", None) or getattr(profile, "preferred_pharmacy_name", None) or "").strip()
    pharmacy_address = (getattr(profile, "preferredPharmacyAddress", None) or getattr(profile, "preferred_pharmacy_address", None) or "").strip()
    return base_complete and bool(pharmacy and pharmacy_address)


@router.get("/")
async def get_my_profile(session: dict = Depends(require_patient)):
    """Get current patient profile. Returns null if not yet created."""
    db = await get_db()
    user = await db.user.find_unique(
        where={"id": session["id"]},
        include={"patientProfile": True},
    )
    if not user or user.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")
    profile = getattr(user, "patientProfile", None)
    intake_confirmed = await _has_confirmed_clinical_intake(db, session["id"])
    if not profile:
        return {
            "profile": None,
            "isComplete": False,
            "intakeConfirmed": intake_confirmed,
            "lastUpdatedAt": (
                getattr(user, "updatedAt", None) or getattr(user, "updated_at", None)
            ).isoformat()
            if (getattr(user, "updatedAt", None) or getattr(user, "updated_at", None))
            else None,
            "user": {
                "firstName": getattr(user, "firstName", None) or getattr(user, "first_name", ""),
                "lastName": getattr(user, "lastName", None) or getattr(user, "last_name", ""),
                "phone": getattr(user, "phone", None),
                "email": user.email,
            },
        }
    return {
        "profile": {
            "preferredName": getattr(profile, "preferredName", None) or getattr(profile, "preferred_name", None),
            "sex": getattr(profile, "sex", None),
            "race": getattr(profile, "race", None),
            "preferredLanguage": getattr(profile, "preferredLanguage", None) or getattr(profile, "preferred_language", None),
            "dateOfBirth": (getattr(profile, "dateOfBirth", None) or getattr(profile, "date_of_birth")).isoformat()[:10] if getattr(profile, "dateOfBirth", None) or getattr(profile, "date_of_birth", None) else None,
            "ssn": getattr(profile, "ssn", None),
            "address": getattr(profile, "address", None),
            "preferredPharmacyName": getattr(profile, "preferredPharmacyName", None) or getattr(profile, "preferred_pharmacy_name", None),
            "preferredPharmacyPhone": getattr(profile, "preferredPharmacyPhone", None) or getattr(profile, "preferred_pharmacy_phone", None),
            "preferredPharmacyAddress": getattr(profile, "preferredPharmacyAddress", None) or getattr(profile, "preferred_pharmacy_address", None),
            "guardian1Name": getattr(profile, "guardian1Name", None) or getattr(profile, "guardian_1_name", None),
            "guardian1Relationship": getattr(profile, "guardian1Relationship", None) or getattr(profile, "guardian_1_relationship", None),
            "guardian1Phone": getattr(profile, "guardian1Phone", None) or getattr(profile, "guardian_1_phone", None),
            "guardian1Email": getattr(profile, "guardian1Email", None) or getattr(profile, "guardian_1_email", None),
            "guardian1Address": getattr(profile, "guardian1Address", None) or getattr(profile, "guardian_1_address", None),
            "guardian2Name": getattr(profile, "guardian2Name", None) or getattr(profile, "guardian_2_name", None),
            "guardian2Relationship": getattr(profile, "guardian2Relationship", None) or getattr(profile, "guardian_2_relationship", None),
            "guardian2Phone": getattr(profile, "guardian2Phone", None) or getattr(profile, "guardian_2_phone", None),
            "guardian2Email": getattr(profile, "guardian2Email", None) or getattr(profile, "guardian_2_email", None),
            "guardian2Address": getattr(profile, "guardian2Address", None) or getattr(profile, "guardian_2_address", None),
            "parentAddress": getattr(profile, "parentAddress", None) or getattr(profile, "parent_address", None),
        },
        "isComplete": _profile_is_complete(user, profile, intake_confirmed),
        "intakeConfirmed": intake_confirmed,
        "lastUpdatedAt": (
            getattr(profile, "updatedAt", None) or getattr(profile, "updated_at", None)
        ).isoformat()
        if (getattr(profile, "updatedAt", None) or getattr(profile, "updated_at", None))
        else (
            (getattr(user, "updatedAt", None) or getattr(user, "updated_at", None)).isoformat()
            if (getattr(user, "updatedAt", None) or getattr(user, "updated_at", None))
            else None
        ),
        "user": {
            "firstName": getattr(user, "firstName", None) or getattr(user, "first_name", ""),
            "lastName": getattr(user, "lastName", None) or getattr(user, "last_name", ""),
            "phone": getattr(user, "phone", None),
            "email": user.email,
        },
    }


@router.get("/check-complete")
async def check_profile_complete(session: dict = Depends(require_patient)):
    """Check if patient profile is complete (required before orientation booking)."""
    db = await get_db()
    user = await db.user.find_unique(
        where={"id": session["id"]},
        include={"patientProfile": True},
    )
    if not user or user.role != "patient":
        return {"isComplete": False}
    profile = getattr(user, "patientProfile", None)
    intake_confirmed = await _has_confirmed_clinical_intake(db, session["id"])
    return {"isComplete": _profile_is_complete(user, profile, intake_confirmed), "intakeConfirmed": intake_confirmed}


@router.put("/")
async def update_my_profile(body: PatientProfileUpdateBody, session: dict = Depends(require_patient)):
    """Create or update patient profile. Mark completed when all required fields present."""
    db = await get_db()
    user = await db.user.find_unique(
        where={"id": session["id"]},
        include={"patientProfile": True},
    )
    if not user or user.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")

    pharmacy_fields_submitted = any(
        v is not None
        for v in (body.preferredPharmacyName, body.preferredPharmacyPhone, body.preferredPharmacyAddress)
    )
    if pharmacy_fields_submitted and not await _has_confirmed_clinical_intake(db, session["id"]):
        raise HTTPException(
            status_code=400,
            detail="Preferred pharmacy can only be set after your clinical intake appointment is confirmed.",
        )

    # Update User fields if provided
    user_updates = {}
    if body.firstName is not None:
        user_updates["firstName"] = body.firstName.strip()
    if body.lastName is not None:
        user_updates["lastName"] = body.lastName.strip()
    if body.phone is not None:
        user_updates["phone"] = body.phone.strip() if body.phone else None
    if user_updates:
        await db.user.update(where={"id": session["id"]}, data=user_updates)

    # Parse DOB
    dob_dt = None
    if body.dateOfBirth:
        try:
            dob_dt = datetime.strptime(body.dateOfBirth.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid dateOfBirth format (use YYYY-MM-DD)")

    profile = getattr(user, "patientProfile", None)
    profile_data = {
        "userId": session["id"],
    }
    if body.preferredName is not None:
        profile_data["preferredName"] = body.preferredName.strip() if body.preferredName else None
    if body.sex is not None:
        profile_data["sex"] = body.sex.strip() if body.sex else None
    if body.race is not None:
        profile_data["race"] = body.race.strip() if body.race else None
    if body.preferredLanguage is not None:
        profile_data["preferredLanguage"] = body.preferredLanguage.strip() if body.preferredLanguage else None
    if body.dateOfBirth is not None:
        profile_data["dateOfBirth"] = f"{dob_dt.isoformat()}T00:00:00.000Z" if dob_dt else None
    if body.ssn is not None:
        profile_data["ssn"] = body.ssn.strip() if body.ssn else None
    if body.address is not None:
        profile_data["address"] = body.address.strip() if body.address else None
    if body.preferredPharmacyName is not None:
        profile_data["preferredPharmacyName"] = body.preferredPharmacyName.strip() if body.preferredPharmacyName else None
    if body.preferredPharmacyPhone is not None:
        profile_data["preferredPharmacyPhone"] = body.preferredPharmacyPhone.strip() if body.preferredPharmacyPhone else None
    if body.preferredPharmacyAddress is not None:
        profile_data["preferredPharmacyAddress"] = body.preferredPharmacyAddress.strip() if body.preferredPharmacyAddress else None
    if body.guardian1Name is not None:
        profile_data["guardian1Name"] = body.guardian1Name.strip() if body.guardian1Name else None
    if body.guardian1Relationship is not None:
        profile_data["guardian1Relationship"] = body.guardian1Relationship.strip() if body.guardian1Relationship else None
    if body.guardian1Phone is not None:
        profile_data["guardian1Phone"] = body.guardian1Phone.strip() if body.guardian1Phone else None
    if body.guardian1Email is not None:
        profile_data["guardian1Email"] = body.guardian1Email.strip() if body.guardian1Email else None
    if body.guardian1Address is not None:
        profile_data["guardian1Address"] = body.guardian1Address.strip() if body.guardian1Address else None
    if body.guardian2Name is not None:
        profile_data["guardian2Name"] = body.guardian2Name.strip() if body.guardian2Name else None
    if body.guardian2Relationship is not None:
        profile_data["guardian2Relationship"] = body.guardian2Relationship.strip() if body.guardian2Relationship else None
    if body.guardian2Phone is not None:
        profile_data["guardian2Phone"] = body.guardian2Phone.strip() if body.guardian2Phone else None
    if body.guardian2Email is not None:
        profile_data["guardian2Email"] = body.guardian2Email.strip() if body.guardian2Email else None
    if body.guardian2Address is not None:
        profile_data["guardian2Address"] = body.guardian2Address.strip() if body.guardian2Address else None

    if profile:
        # Update existing profile - remove userId for update
        update_data = {k: v for k, v in profile_data.items() if k != "userId"}
        if update_data:
            await db.patientprofile.update(where={"id": profile.id}, data=update_data)
    else:
        # Create new profile
        await db.patientprofile.create(data=profile_data)

    # Re-fetch and check if complete; set completedAt if so
    user = await db.user.find_unique(where={"id": session["id"]}, include={"patientProfile": True})
    profile = getattr(user, "patientProfile", None)
    intake_confirmed = await _has_confirmed_clinical_intake(db, session["id"])
    if profile and _profile_is_complete(user, profile, intake_confirmed) and not getattr(profile, "completedAt", None):
        await db.patientprofile.update(
            where={"id": profile.id},
            data={"completedAt": datetime.now(timezone.utc)},
        )

    # Ensure GCP FHIR Patient exists and stays in sync
    try:
        fhir_id = await ensure_fhir_patient_for_user(db, session["id"])
        if fhir_id and profile:
            payload = build_fhir_patient_payload(user, profile)
            payload["id"] = fhir_id
            update_fhir_resource("Patient", fhir_id, payload)
    except Exception:
        pass  # Non-fatal; FHIR may be disabled

    return {"ok": True}

#!/usr/bin/env python3
"""
Remove all patient-related records for a given email, including FHIR-stored questionnaire data.

Usage:
  From backend/ with .env loaded (use .venv):
    .venv/bin/python scripts/remove_patient_by_email.py
    .venv/bin/python scripts/remove_patient_by_email.py other@example.com

  Or set PATIENT_EMAIL env var:
    PATIENT_EMAIL=maneeshghanta@gmail.com .venv/bin/python scripts/remove_patient_by_email.py

Deletes (in order):
  - FHIR QuestionnaireResponse for each PatientRequest (pre-screening)
  - FHIR QuestionnaireResponse for each PatientFormAssignment (assigned forms)
  - Appointments where the patient is this user
  - IntakeSubmission for this user
  - PatientRequest(s) with this email (and clears user link if approved)
  - User (patient) with this email
"""
import asyncio
import os
import sys

# Load backend .env and make prisma importable
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)
os.chdir(backend_dir)

from dotenv import load_dotenv
load_dotenv()

# Default email if none provided
DEFAULT_EMAIL = "maneeshghanta@gmail.com"


async def main() -> None:
    email = (os.environ.get("PATIENT_EMAIL") or "").strip()
    if not email and len(sys.argv) > 1:
        email = sys.argv[1].strip()
    if not email:
        email = DEFAULT_EMAIL

    email = email.lower()
    print(f"Removing all patient records for: {email}")

    from app.healthcare_fhir import delete_questionnaire_response
    from prisma import Prisma

    db = Prisma()
    await db.connect()
    try:
        # 1. Find and delete FHIR data for PatientRequests with this email (BEFORE deleting records)
        patient_requests = await db.patientrequest.find_many(
            where={"email": email},
        )
        for pr in patient_requests:
            fhir_id = getattr(pr, "fhirQuestionnaireResponseId", None) or getattr(
                pr, "fhir_questionnaire_response_id", None
            )
            if fhir_id and delete_questionnaire_response(fhir_id):
                print(f"  Deleted FHIR QuestionnaireResponse {fhir_id} (PatientRequest).")

        # 2. Find patient user and delete FHIR data for form assignments (BEFORE deleting user)
        user = await db.user.find_first(
            where={"email": email, "role": "patient"},
        )
        if user:
            user_id = user.id
            # Delete FHIR resources for form assignments
            form_assignments = await db.patientformassignment.find_many(
                where={"patientId": user_id},
            )
            for fa in form_assignments:
                fhir_id = getattr(fa, "fhirResponseId", None) or getattr(fa, "fhir_response_id", None)
                if fhir_id and delete_questionnaire_response(fhir_id):
                    print(f"  Deleted FHIR QuestionnaireResponse {fhir_id} (form assignment).")

            # Delete appointments
            deleted_appts = await db.appointment.delete_many(
                where={"patientId": user_id},
            )
            print(f"  Deleted {deleted_appts} appointment(s).")
            # Delete intake submission
            intake = await db.intakesubmission.find_unique(where={"userId": user_id})
            if intake:
                await db.intakesubmission.delete(where={"id": intake.id})
                print("  Deleted intake submission.")
            # Unlink patient request from user
            await db.patientrequest.update_many(
                where={"userId": user_id},
                data={"userId": None},
            )
            # Delete the patient user
            await db.user.delete(where={"id": user_id})
            print("  Deleted patient user.")
        else:
            print("  No patient user found with this email.")

        # 3. Delete patient request(s) with this email
        deleted_requests = await db.patientrequest.delete_many(
            where={"email": email},
        )
        print(f"  Deleted {deleted_requests} patient request(s) with this email.")

        print("Done.")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

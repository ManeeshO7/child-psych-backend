-- Add GCP FHIR Patient ID to users for eRx/OpenEMR integration (source of truth link)
ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "fhir_patient_id" TEXT;

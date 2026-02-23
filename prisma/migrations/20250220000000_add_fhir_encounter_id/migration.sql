-- Add FHIR Encounter ID to appointments for HIPAA-compliant encounter notes storage
ALTER TABLE "appointments" ADD COLUMN IF NOT EXISTS "fhir_encounter_id" TEXT;

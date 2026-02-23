-- =============================================================================
-- TelePsych Production Schema (single file for GCP Cloud SQL / prod migration)
-- Run this on an EMPTY database only. Creates all tables and columns in order.
-- Users table includes all columns (no separate ALTERs).
-- =============================================================================

-- 1. USERS (all columns in one go)
CREATE TABLE IF NOT EXISTS "users" (
    "id" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "password_hash" TEXT,
    "first_name" TEXT NOT NULL,
    "last_name" TEXT NOT NULL,
    "phone" TEXT,
    "role" TEXT NOT NULL DEFAULT 'patient',
    "email_verified" TIMESTAMP(3),
    "temp_password_expires_at" TIMESTAMP(3),
    "must_reset_password" BOOLEAN NOT NULL DEFAULT false,
    "password_reset_token" TEXT,
    "password_reset_token_expires_at" TIMESTAMP(3),
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,
    "google_refresh_token" TEXT,
    "google_calendar_id" TEXT,
    "stripe_customer_id" TEXT,
    "allowed_followup_types" JSONB,
    "rejected_after_orientation" BOOLEAN,
    CONSTRAINT "users_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX IF NOT EXISTS "users_email_key" ON "users"("email");
CREATE UNIQUE INDEX IF NOT EXISTS "users_stripe_customer_id_key" ON "users"("stripe_customer_id");

-- 2. PATIENT_PROFILES
CREATE TABLE IF NOT EXISTS "patient_profiles" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "sex" TEXT,
    "date_of_birth" TIMESTAMP(3),
    "ssn" TEXT,
    "address" TEXT,
    "preferred_pharmacy_name" TEXT,
    "preferred_pharmacy_phone" TEXT,
    "preferred_pharmacy_address" TEXT,
    "guardian_1_name" TEXT,
    "guardian_1_relationship" TEXT,
    "guardian_1_phone" TEXT,
    "guardian_1_address" TEXT,
    "guardian_2_name" TEXT,
    "guardian_2_relationship" TEXT,
    "guardian_2_phone" TEXT,
    "guardian_2_address" TEXT,
    "completed_at" TIMESTAMP(3),
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "patient_profiles_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX IF NOT EXISTS "patient_profiles_user_id_key" ON "patient_profiles"("user_id");
ALTER TABLE "patient_profiles" DROP CONSTRAINT IF EXISTS "patient_profiles_user_id_fkey";
ALTER TABLE "patient_profiles" ADD CONSTRAINT "patient_profiles_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- 3. PATIENT_REQUESTS
CREATE TABLE IF NOT EXISTS "patient_requests" (
    "id" TEXT NOT NULL,
    "first_name" TEXT NOT NULL,
    "last_name" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "phone" TEXT NOT NULL,
    "notes" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "rejected_note" TEXT,
    "reviewed_at" TIMESTAMP(3),
    "reviewed_by" TEXT,
    "consent_at" TIMESTAMP(3),
    "questionnaire_data" JSONB,
    "fhir_questionnaire_response_id" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "user_id" TEXT,
    CONSTRAINT "patient_requests_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX IF NOT EXISTS "patient_requests_user_id_key" ON "patient_requests"("user_id");
ALTER TABLE "patient_requests" DROP CONSTRAINT IF EXISTS "patient_requests_user_id_fkey";
ALTER TABLE "patient_requests" ADD CONSTRAINT "patient_requests_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- 4. PATIENT_DOCUMENTS
CREATE TABLE IF NOT EXISTS "patient_documents" (
    "id" TEXT NOT NULL,
    "user_id" TEXT NOT NULL,
    "display_name" TEXT NOT NULL,
    "file_name" TEXT NOT NULL,
    "gcs_path" TEXT NOT NULL,
    "content_type" TEXT NOT NULL DEFAULT 'application/pdf',
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "patient_documents_pkey" PRIMARY KEY ("id")
);
ALTER TABLE "patient_documents" DROP CONSTRAINT IF EXISTS "patient_documents_user_id_fkey";
ALTER TABLE "patient_documents" ADD CONSTRAINT "patient_documents_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- 5. FORMS
CREATE TABLE IF NOT EXISTS "forms" (
    "id" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "description" TEXT,
    "type" TEXT NOT NULL,
    "questionnaire_key" TEXT,
    "template_gcs_path" TEXT,
    "is_active" BOOLEAN NOT NULL DEFAULT true,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "forms_pkey" PRIMARY KEY ("id")
);

-- 6. APPOINTMENTS (depends on users)
CREATE TABLE IF NOT EXISTS "appointments" (
    "id" TEXT NOT NULL,
    "patient_id" TEXT NOT NULL,
    "doctor_id" TEXT NOT NULL,
    "scheduled_at" TIMESTAMP(3) NOT NULL,
    "duration_minutes" INTEGER NOT NULL DEFAULT 60,
    "status" TEXT NOT NULL DEFAULT 'scheduled',
    "type" TEXT NOT NULL DEFAULT 'followup',
    "notes" TEXT,
    "meet_link" TEXT,
    "google_calendar_event_id" TEXT,
    "fhir_encounter_id" TEXT,
    "require_forms_before_confirm" BOOLEAN NOT NULL DEFAULT true,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "appointments_pkey" PRIMARY KEY ("id")
);
ALTER TABLE "appointments" DROP CONSTRAINT IF EXISTS "appointments_patient_id_fkey";
ALTER TABLE "appointments" ADD CONSTRAINT "appointments_patient_id_fkey" FOREIGN KEY ("patient_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "appointments" DROP CONSTRAINT IF EXISTS "appointments_doctor_id_fkey";
ALTER TABLE "appointments" ADD CONSTRAINT "appointments_doctor_id_fkey" FOREIGN KEY ("doctor_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- 7. DOCTOR_AVAILABILITY
CREATE TABLE IF NOT EXISTS "doctor_availability" (
    "id" TEXT NOT NULL,
    "doctor_id" TEXT NOT NULL,
    "date" DATE NOT NULL,
    "start_time" TEXT NOT NULL,
    "end_time" TEXT NOT NULL,
    "allowed_durations_minutes" JSONB,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "doctor_availability_pkey" PRIMARY KEY ("id")
);
ALTER TABLE "doctor_availability" DROP CONSTRAINT IF EXISTS "doctor_availability_doctor_id_fkey";
ALTER TABLE "doctor_availability" ADD CONSTRAINT "doctor_availability_doctor_id_fkey" FOREIGN KEY ("doctor_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- 8. DOCTOR_BOOKABLE_SLOTS
CREATE TABLE IF NOT EXISTS "doctor_bookable_slots" (
    "id" TEXT NOT NULL,
    "doctor_id" TEXT NOT NULL,
    "date" DATE NOT NULL,
    "start_time" TEXT NOT NULL,
    "duration_minutes" INTEGER NOT NULL,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "doctor_bookable_slots_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX IF NOT EXISTS "doctor_bookable_slots_doctor_id_date_start_time_duration_minutes_key" ON "doctor_bookable_slots"("doctor_id", "date", "start_time", "duration_minutes");
ALTER TABLE "doctor_bookable_slots" DROP CONSTRAINT IF EXISTS "doctor_bookable_slots_doctor_id_fkey";
ALTER TABLE "doctor_bookable_slots" ADD CONSTRAINT "doctor_bookable_slots_doctor_id_fkey" FOREIGN KEY ("doctor_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- 9. PATIENT_FORM_ASSIGNMENTS
CREATE TABLE IF NOT EXISTS "patient_form_assignments" (
    "id" TEXT NOT NULL,
    "patient_id" TEXT NOT NULL,
    "form_id" TEXT NOT NULL,
    "appointment_id" TEXT,
    "assigned_by" TEXT NOT NULL,
    "assigned_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "completed_at" TIMESTAMP(3),
    "fhir_response_id" TEXT,
    "uploaded_gcs_path" TEXT,
    CONSTRAINT "patient_form_assignments_pkey" PRIMARY KEY ("id")
);
ALTER TABLE "patient_form_assignments" DROP CONSTRAINT IF EXISTS "patient_form_assignments_patient_id_fkey";
ALTER TABLE "patient_form_assignments" ADD CONSTRAINT "patient_form_assignments_patient_id_fkey" FOREIGN KEY ("patient_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "patient_form_assignments" DROP CONSTRAINT IF EXISTS "patient_form_assignments_form_id_fkey";
ALTER TABLE "patient_form_assignments" ADD CONSTRAINT "patient_form_assignments_form_id_fkey" FOREIGN KEY ("form_id") REFERENCES "forms"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "patient_form_assignments" DROP CONSTRAINT IF EXISTS "patient_form_assignments_appointment_id_fkey";
ALTER TABLE "patient_form_assignments" ADD CONSTRAINT "patient_form_assignments_appointment_id_fkey" FOREIGN KEY ("appointment_id") REFERENCES "appointments"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- 10. FORM_SUBMISSIONS
CREATE TABLE IF NOT EXISTS "form_submissions" (
    "id" TEXT NOT NULL,
    "assignment_id" TEXT NOT NULL,
    "submitted_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "notes" TEXT,
    CONSTRAINT "form_submissions_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX IF NOT EXISTS "form_submissions_assignment_id_key" ON "form_submissions"("assignment_id");
ALTER TABLE "form_submissions" DROP CONSTRAINT IF EXISTS "form_submissions_assignment_id_fkey";
ALTER TABLE "form_submissions" ADD CONSTRAINT "form_submissions_assignment_id_fkey" FOREIGN KEY ("assignment_id") REFERENCES "patient_form_assignments"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- Prisma migration history (optional: create so prisma migrate deploy sees a baseline)
CREATE TABLE IF NOT EXISTS "_prisma_migrations" (
    "id" VARCHAR(36) NOT NULL,
    "checksum" VARCHAR(64) NOT NULL,
    "finished_at" TIMESTAMPTZ,
    "migration_name" VARCHAR(255) NOT NULL,
    "logs" TEXT,
    "rolled_back_at" TIMESTAMPTZ,
    "started_at" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "applied_steps_count" INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT "_prisma_migrations_pkey" PRIMARY KEY ("id")
);

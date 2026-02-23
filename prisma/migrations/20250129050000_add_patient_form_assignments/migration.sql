-- Add columns to appointments (missing from 20250129; added here before patient_form_assignments)
ALTER TABLE "appointments" ADD COLUMN IF NOT EXISTS "meet_link" TEXT;
ALTER TABLE "appointments" ADD COLUMN IF NOT EXISTS "require_forms_before_confirm" BOOLEAN NOT NULL DEFAULT true;

-- CreateTable
CREATE TABLE "patient_form_assignments" (
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

-- CreateTable
CREATE TABLE "form_submissions" (
    "id" TEXT NOT NULL,
    "assignment_id" TEXT NOT NULL,
    "submitted_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "notes" TEXT,

    CONSTRAINT "form_submissions_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "form_submissions_assignment_id_key" ON "form_submissions"("assignment_id");

-- AddForeignKey
ALTER TABLE "patient_form_assignments" ADD CONSTRAINT "patient_form_assignments_patient_id_fkey" FOREIGN KEY ("patient_id") REFERENCES "users"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "patient_form_assignments" ADD CONSTRAINT "patient_form_assignments_form_id_fkey" FOREIGN KEY ("form_id") REFERENCES "forms"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "patient_form_assignments" ADD CONSTRAINT "patient_form_assignments_appointment_id_fkey" FOREIGN KEY ("appointment_id") REFERENCES "appointments"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "form_submissions" ADD CONSTRAINT "form_submissions_assignment_id_fkey" FOREIGN KEY ("assignment_id") REFERENCES "patient_form_assignments"("id") ON DELETE CASCADE ON UPDATE CASCADE;

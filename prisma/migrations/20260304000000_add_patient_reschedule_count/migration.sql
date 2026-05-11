-- AlterTable
ALTER TABLE "appointments" ADD COLUMN IF NOT EXISTS "patient_reschedule_count" INTEGER NOT NULL DEFAULT 0;

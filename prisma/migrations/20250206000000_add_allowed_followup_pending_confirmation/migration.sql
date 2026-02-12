-- AlterTable
ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "allowed_followup_types" JSONB;

-- Note: Appointment status "pending_confirmation" is a new string value; no schema change needed.
-- Existing status column accepts any string.

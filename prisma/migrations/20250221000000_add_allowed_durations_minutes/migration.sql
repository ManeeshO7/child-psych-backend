-- Add allowed_durations_minutes to doctor_availability (e.g. [30, 45, 60]; null = all)
ALTER TABLE "doctor_availability" ADD COLUMN IF NOT EXISTS "allowed_durations_minutes" JSONB;

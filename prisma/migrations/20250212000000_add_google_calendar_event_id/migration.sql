-- AlterTable
ALTER TABLE "appointments" ADD COLUMN IF NOT EXISTS "google_calendar_event_id" TEXT;

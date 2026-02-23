-- Insert PSC-17 form so it appears in "Assign Forms" in the doctor portal.
-- Run from backend/ with: npx dotenv -e .env -- prisma db execute --file prisma/insert_psc17_form.sql
-- Or run this SQL in any PostgreSQL client connected to your DATABASE_URL.

INSERT INTO forms (
  id,
  title,
  description,
  type,
  questionnaire_key,
  template_gcs_path,
  is_active,
  created_at,
  updated_at
)
SELECT
  gen_random_uuid()::text,
  'Pediatric Symptom Checklist (PSC-17)',
  'Screening for emotional, attention, and behavior difficulties. Options: Never, Sometimes, Often.',
  'questionnaire',
  'psc17',
  NULL,
  true,
  now(),
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM forms WHERE questionnaire_key = 'psc17'
);

-- Insert PHQ-9 form so it appears in "Assign Forms" in the doctor portal.
-- Run from backend/ with:
-- npx dotenv -e .env -- prisma db execute --file prisma/insert_phq9_form.sql --schema prisma/schema.prisma

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
  'PHQ-9 (Adults 18+)',
  'Adult depression screening for ages 18+.',
  'questionnaire',
  'phq9',
  NULL,
  true,
  now(),
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM forms WHERE questionnaire_key = 'phq9'
);


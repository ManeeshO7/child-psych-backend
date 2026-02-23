-- Insert PHQ-A form so it appears in "Assign Forms" in the doctor portal.
-- Run from backend/ with: npx dotenv -e .env -- prisma db execute --file prisma/insert_phqa_form.sql --schema prisma/schema.prisma
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
  'PHQ-A (Adolescent Depression Questionnaire)',
  'Over the last 2 weeks, how often have you been bothered by the following problems? Options: Not at all, Several days, More than half the days, Nearly every day.',
  'questionnaire',
  'phqa',
  NULL,
  true,
  now(),
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM forms WHERE questionnaire_key = 'phqa'
);

-- Insert DSM-Based ADHD Teacher Rating Scale so it appears in "Assign Forms" in the doctor portal.
-- Run from backend/ with:
-- npx dotenv -e .env -- prisma db execute --file prisma/insert_adhd_teacher_rating_form.sql --schema prisma/schema.prisma

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
  'DSM-Based ADHD Teacher Rating Scale',
  'ADHD symptom rating scale (teacher report) for the past 6 months.',
  'questionnaire',
  'adhd_teacher_rating',
  NULL,
  true,
  now(),
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM forms WHERE questionnaire_key = 'adhd_teacher_rating'
);


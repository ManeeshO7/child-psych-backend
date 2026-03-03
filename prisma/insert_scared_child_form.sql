-- Insert SCARED Child form so it appears in "Assign Forms" in the doctor portal.
-- Run from backend/ with:
-- npx dotenv -e .env -- prisma db execute --file prisma/insert_scared_child_form.sql --schema prisma/schema.prisma

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
  'SCARED - Child Version (Ages 8-18)',
  'Child anxiety screening for ages 8-18.',
  'questionnaire',
  'scared_child',
  NULL,
  true,
  now(),
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM forms WHERE questionnaire_key = 'scared_child'
);


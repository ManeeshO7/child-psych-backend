-- Insert SCARED Parent form so it appears in "Assign Forms" in the doctor portal.
-- Run from backend/ with:
-- npx dotenv -e .env -- prisma db execute --file prisma/insert_scared_parent_form.sql --schema prisma/schema.prisma

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
  'SCARED - Parent Version',
  'Parent-report anxiety screening over the past 3 months.',
  'questionnaire',
  'scared_parent',
  NULL,
  true,
  now(),
  now()
WHERE NOT EXISTS (
  SELECT 1 FROM forms WHERE questionnaire_key = 'scared_parent'
);


# Production SQL migration (single file)

Use **`migrations/production_full_schema.sql`** to set up a **fresh** production PostgreSQL database (e.g. GCP Cloud SQL) in one step.

## What it does

- Creates all tables with **full columns** (no missing columns).
- **`users`** table includes: id, email, password_hash, first_name, last_name, phone, role, email_verified, temp_password_expires_at, must_reset_password, password_reset_token, password_reset_token_expires_at, created_at, updated_at, google_refresh_token, google_calendar_id, stripe_customer_id, allowed_followup_types, rejected_after_orientation.
- Also creates: patient_profiles, patient_requests, patient_documents, forms, appointments, doctor_availability, doctor_bookable_slots, patient_form_assignments, form_submissions (and indexes/FKs).

*Note: “Patient profile”, “Appointments as patient”, etc. are **relations** (other tables linked by foreign keys), not columns on `users`. This script creates all those related tables.*

## How to run on prod (empty database)

1. Create the database and user in Cloud SQL (or your prod Postgres).
2. Connect with `psql` or any PostgreSQL client using your prod `DATABASE_URL`.
3. Run the script:

   ```bash
   psql "$DATABASE_URL" -f prisma/migrations/production_full_schema.sql
   ```

   Or from Cloud Shell / Cloud SQL Proxy:

   ```bash
   psql "postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require" -f prisma/migrations/production_full_schema.sql
   ```

4. Then run `prisma generate` in your app so the Prisma client matches the schema.

## Re-running (e.g. existing dev DB)

The script uses `CREATE TABLE IF NOT EXISTS` and `DROP CONSTRAINT IF EXISTS` before adding foreign keys, so it can be re-run. Tables that already exist are left as-is; only missing tables are created. For existing tables, ensure any new columns you need are added manually or via separate migrations.

## Prisma migrate deploy vs this file

- **`npx prisma migrate deploy`** – Use for **incremental** migrations (applies the migration history in `prisma/migrations/`). Best when you already use Prisma migrations in dev/staging.
- **`production_full_schema.sql`** – Use for a **one-off** setup of a **new** prod database when you want a single file to run and not depend on migration history.

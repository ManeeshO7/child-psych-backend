#!/usr/bin/env python3
"""
Seed initial forms into the database.

Usage:
  From backend/ with .env loaded:
    python scripts/seed_forms.py

Creates:
  - Questionnaire forms (stored in FHIR)
  - PDF upload forms (templates stored in GCS)
"""
import asyncio
import os
import sys

# Load backend .env and make prisma importable
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)
os.chdir(backend_dir)

from dotenv import load_dotenv
load_dotenv()

from app.db import connect_db, disconnect_db, get_db


FORMS_TO_SEED = [
    {
        "title": "PHQ-A (Adolescents 11-17 years)",
        "description": "Adolescent depression screening for ages 11-17. Choose one response per item.",
        "type": "questionnaire",
        "questionnaireKey": "phqa",
    },
    {
        "title": "PHQ-9 (Adults 18+)",
        "description": "Adult depression screening for ages 18+.",
        "type": "questionnaire",
        "questionnaireKey": "phq9",
    },
    {
        "title": "SCARED - Child Version (Ages 8-18)",
        "description": "Child anxiety screening for ages 8-18.",
        "type": "questionnaire",
        "questionnaireKey": "scared_child",
    },
    {
        "title": "SCARED - Parent Version",
        "description": "Parent-report anxiety screening over the past 3 months.",
        "type": "questionnaire",
        "questionnaireKey": "scared_parent",
    },
    {
        "title": "DSM-Based ADHD Parent Rating Scale",
        "description": "ADHD symptom rating scale (parent report) for the past 6 months.",
        "type": "questionnaire",
        "questionnaireKey": "adhd_parent_rating",
    },
    {
        "title": "DSM-Based ADHD Teacher Rating Scale",
        "description": "ADHD symptom rating scale (teacher report) for the past 6 months.",
        "type": "questionnaire",
        "questionnaireKey": "adhd_teacher_rating",
    },
]


async def seed_forms():
    await connect_db()
    try:
        db = await get_db()
        
        created = 0
        skipped = 0
        
        for form_data in FORMS_TO_SEED:
            # Check if form already exists (by title)
            existing = await db.form.find_first(where={"title": form_data["title"]})
            if existing:
                print(f"⏭️  Skipping '{form_data['title']}' (already exists)")
                skipped += 1
                continue
            
            # Create form
            form = await db.form.create(data=form_data)
            print(f"✅ Created form: {form.title} (ID: {form.id}, Type: {form.type})")
            created += 1
        
        print(f"\n📊 Summary: {created} created, {skipped} skipped")
        
    finally:
        await disconnect_db()


if __name__ == "__main__":
    asyncio.run(seed_forms())

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
        "title": "Clinical Workflow Blueprint",
        "description": "Download, complete, and upload this PDF form.",
        "type": "pdf_upload",
        "templateGcsPath": "forms/templates/Clinical_Workflow_Blueprint.pdf",
    },
    {
        "title": "Design Flow",
        "description": "Download, complete, and upload this PDF form.",
        "type": "pdf_upload",
        "templateGcsPath": "forms/templates/Design_Flow.pdf",
    },
    {
        "title": "Follow-up Assessment Questionnaire",
        "description": "Complete this questionnaire about your current symptoms and progress.",
        "type": "questionnaire",
        "questionnaireKey": "followup_assessment",
    },
    {
        "title": "Medication Review",
        "description": "Review your current medications and any side effects.",
        "type": "questionnaire",
        "questionnaireKey": "medication_review",
    },
    {
        "title": "Mood Tracking",
        "description": "Track your mood and daily activities.",
        "type": "questionnaire",
        "questionnaireKey": "mood_tracking",
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

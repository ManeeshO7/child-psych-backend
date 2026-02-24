#!/usr/bin/env python3
"""
Seed script: create doctor user from DOCTOR_EMAIL and DOCTOR_PASSWORD.
Run from backend/: python prisma/seed.py  (with .env loaded or env vars set)
"""
import asyncio
import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _backend_dir)

from dotenv import load_dotenv
load_dotenv(os.path.join(_backend_dir, ".env"))

from prisma import Prisma


async def main() -> None:
    doctor_email = (os.environ.get("DOCTOR_EMAIL") or "").strip()
    doctor_password = (os.environ.get("DOCTOR_PASSWORD") or "").strip()
    if not doctor_email or not doctor_password:
        return

    from app.auth import hash_password

    db = Prisma()
    await db.connect()
    try:
        existing = await db.user.find_first(
            where={"email": doctor_email.lower(), "role": "doctor"},
        )
        if existing:
            return
        hashed = hash_password(doctor_password)
        await db.user.create(
            data={
                "email": doctor_email.lower(),
                "passwordHash": hashed,
                "firstName": "Doctor",
                "lastName": "Admin",
                "role": "doctor",
            },
        )
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

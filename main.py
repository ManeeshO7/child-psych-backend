import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

# Ensure app and email send logs are visible (e.g. when doctor approves and Resend is used)
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(name)s: %(message)s",
)
logging.getLogger("app").setLevel(logging.INFO)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import connect_db, disconnect_db
from app.routers import auth, contact, request_access, patient_requests, appointments, availability, payments, patient_forms, patient_profile, patient_documents


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    try:
        yield
    finally:
        await disconnect_db()


app = FastAPI(
    title="TelePsych API",
    description="Backend API for TelePsych",
    version="0.1.0",
    lifespan=lifespan,
)

frontend_origin = os.environ.get("FRONTEND_ORIGIN", "http://localhost:3100")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(contact.router)
app.include_router(request_access.router)
app.include_router(patient_requests.router)
app.include_router(appointments.router)
app.include_router(availability.router)
app.include_router(payments.router)
app.include_router(patient_forms.router)
app.include_router(patient_profile.router)
app.include_router(patient_documents.router)


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

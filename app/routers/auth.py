import os

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.auth import (
    create_session_payload,
    generate_reset_token,
    hash_password,
    verify_password,
)
from app.db import get_db
from app.dependencies import get_session
from app.email_service import send_password_reset_email

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE = "telepsych_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7


def _is_production() -> bool:
    env = os.environ.get("ENV", "").lower()
    if env in ("production", "prod"):
        return True
    return os.environ.get("FRONTEND_ORIGIN", "").strip().lower().startswith("https://")


class DoctorLoginBody(BaseModel):
    email: str
    password: str


class PatientLoginBody(BaseModel):
    email: str
    password: str


class ResetPasswordBody(BaseModel):
    token: str
    newPassword: str


class RequestPasswordResetBody(BaseModel):
    email: str


def set_session_cookie(response: Response, user_id: str, role: str, token: str | None = None) -> str:
    token = token or create_session_payload(user_id, role)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_is_production(),
    )
    # Proxy (Next.js) can't always read Set-Cookie from fetch(); send token in header so it can set the cookie
    response.headers["X-Session-Token"] = token
    return token


@router.post("/doctor-login")
async def doctor_login(body: DoctorLoginBody, response: Response):
    db = await get_db()
    user = await db.user.find_first(
        where={"email": body.email.lower().strip(), "role": "doctor"}
    )
    pw_hash = getattr(user, "password_hash", None) or getattr(user, "passwordHash", None) if user else None
    if not user or not pw_hash:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not verify_password(body.password, pw_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = set_session_cookie(response, user.id, "doctor")
    return {"success": True, "token": token}


@router.post("/patient-login")
async def patient_login(body: PatientLoginBody, response: Response):
    db = await get_db()
    user = await db.user.find_first(
        where={"email": body.email.lower().strip(), "role": "patient"}
    )
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    pw_hash = getattr(user, "password_hash", None) or getattr(user, "passwordHash", None)
    if not pw_hash:
        raise HTTPException(
            status_code=401,
            detail="Password not set. Please use the secure link from your approval email to create your password.",
        )
    if not verify_password(body.password, pw_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = set_session_cookie(response, user.id, "patient")
    return {"success": True, "token": token}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return {"success": True}


@router.get("/session")
async def session(session_user: dict = Depends(get_session)):
    if not session_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session_user


@router.post("/reset-password")
async def reset_password(body: ResetPasswordBody):
    if not body.token or not body.newPassword or len(body.newPassword) < 8:
        raise HTTPException(
            status_code=400,
            detail="Valid token and password (min 8 characters) required",
        )
    db = await get_db()
    from datetime import datetime, timezone
    user = await db.user.find_first(
        where={
            "passwordResetToken": body.token,
            "passwordResetTokenExpiresAt": {"gt": datetime.now(timezone.utc)},
        }
    )
    if not user:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset link. Please request a new one.",
        )
    hashed = hash_password(body.newPassword)
    await db.user.update(
        where={"id": user.id},
        data={
            "passwordHash": hashed,
            "mustResetPassword": False,
            "passwordResetToken": None,
            "passwordResetTokenExpiresAt": None,
        },
    )
    return {"success": True}


@router.post("/doctor/request-password-reset")
async def request_doctor_password_reset(body: RequestPasswordResetBody):
    """
    Doctor self-serve password reset.
    Always returns success to avoid leaking whether an email exists.
    """
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    db = await get_db()
    user = await db.user.find_first(where={"email": email, "role": "doctor"})
    if not user:
        return {"success": True}

    from datetime import datetime, timezone, timedelta
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    reset_token = generate_reset_token()
    await db.user.update(
        where={"id": user.id},
        data={
            "passwordResetToken": reset_token,
            "passwordResetTokenExpiresAt": expires_at,
            "mustResetPassword": True,
        },
    )

    first = getattr(user, "first_name", None) or getattr(user, "firstName", "") or ""
    ok, err = send_password_reset_email(
        to=user.email,
        first_name=first,
        reset_token=reset_token,
        portal="doctor",
        expires_hours=24,
    )
    if not ok:
        import logging
        logging.getLogger(__name__).warning("Doctor password reset email failed: %s", err)
    return {"success": True}

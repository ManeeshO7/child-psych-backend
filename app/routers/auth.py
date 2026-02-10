import base64
import os

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth import (
    create_session_payload,
    generate_reset_token,
    hash_password,
    verify_password,
)
from app.db import get_db
from app.dependencies import get_session, require_doctor
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


class UpdateMeBody(BaseModel):
    firstName: str | None = None
    lastName: str | None = None
    phone: str | None = None


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


@router.get("/me")
async def me(session_user: dict = Depends(get_session)):
    """Return the logged-in user's profile (safe fields)."""
    if not session_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db = await get_db()
    user = await db.user.find_unique(where={"id": session_user["id"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": user.id,
        "role": user.role,
        "email": user.email,
        "firstName": getattr(user, "first_name", None) or getattr(user, "firstName", ""),
        "lastName": getattr(user, "last_name", None) or getattr(user, "lastName", ""),
        "phone": getattr(user, "phone", None),
        "createdAt": (getattr(user, "createdAt", None) or getattr(user, "created_at", None)).isoformat()
        if (getattr(user, "createdAt", None) or getattr(user, "created_at", None))
        else None,
    }


@router.patch("/me")
async def update_me(body: UpdateMeBody, session_user: dict = Depends(get_session)):
    """Update logged-in user's profile (safe editable fields)."""
    if not session_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db = await get_db()
    user = await db.user.find_unique(where={"id": session_user["id"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    data: dict = {}
    if body.firstName is not None:
        data["firstName"] = body.firstName.strip()
    if body.lastName is not None:
        data["lastName"] = body.lastName.strip()
    if body.phone is not None:
        data["phone"] = body.phone.strip() or None
    if not data:
        return {"ok": True}

    updated = await db.user.update(where={"id": user.id}, data=data)
    return {
        "id": updated.id,
        "role": updated.role,
        "email": updated.email,
        "firstName": getattr(updated, "first_name", None) or getattr(updated, "firstName", ""),
        "lastName": getattr(updated, "last_name", None) or getattr(updated, "lastName", ""),
        "phone": getattr(updated, "phone", None),
    }


@router.get("/calendar-status")
async def calendar_status(session: dict = Depends(require_doctor)):
    """Doctor: whether Google Calendar is connected (for Meet links when patients book)."""
    db = await get_db()
    user = await db.user.find_unique(where={"id": session["id"]})
    if not user:
        raise HTTPException(status_code=401, detail="Not found")
    refresh = getattr(user, "googleRefreshToken", None) or getattr(user, "google_refresh_token", None)
    return {"connected": bool(refresh)}


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


# ----- Google Calendar OAuth (doctor) -----

def _google_oauth_redirect_uri() -> str:
    origin = (os.environ.get("FRONTEND_ORIGIN") or "http://localhost:3100").rstrip("/")
    return f"{origin}/api/auth/google/callback"


@router.get("/google")
async def google_oauth_start(session: dict = Depends(require_doctor)):
    """Redirect doctor to Google consent screen. State = doctor id for callback."""
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": [_google_oauth_redirect_uri()],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/calendar.events"],
    )
    flow.redirect_uri = _google_oauth_redirect_uri()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=base64.urlsafe_b64encode(session["id"].encode()).decode().rstrip("="),
    )
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/google/callback")
async def google_oauth_callback(code: str | None = None, state: str | None = None):
    """Exchange code for tokens and store refresh_token for doctor. Redirect to doctor dashboard."""
    if not code or not state:
        return RedirectResponse(
            url=os.environ.get("FRONTEND_ORIGIN", "http://localhost:3100").rstrip("/") + "/doctor?calendar=error",
            status_code=302,
        )
    try:
        pad = 4 - len(state) % 4
        if pad != 4:
            state = state + ("=" * pad)
        doctor_id = base64.urlsafe_b64decode(state).decode()
    except Exception:
        doctor_id = None
    if not doctor_id:
        return RedirectResponse(
            url=os.environ.get("FRONTEND_ORIGIN", "http://localhost:3100").rstrip("/") + "/doctor?calendar=error",
            status_code=302,
        )
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        return RedirectResponse(
            url=os.environ.get("FRONTEND_ORIGIN", "http://localhost:3100").rstrip("/") + "/doctor?calendar=error",
            status_code=302,
        )
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": [_google_oauth_redirect_uri()],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/calendar.events"],
    )
    flow.redirect_uri = _google_oauth_redirect_uri()
    try:
        flow.fetch_token(code=code)
        credentials = flow.credentials
        refresh_token = credentials.refresh_token
        if not refresh_token:
            return RedirectResponse(
                url=os.environ.get("FRONTEND_ORIGIN", "http://localhost:3100").rstrip("/") + "/doctor?calendar=error",
                status_code=302,
            )
    except Exception:
        return RedirectResponse(
            url=os.environ.get("FRONTEND_ORIGIN", "http://localhost:3100").rstrip("/") + "/doctor?calendar=error",
            status_code=302,
        )
    db = await get_db()
    user = await db.user.find_unique(where={"id": doctor_id, "role": "doctor"})
    if not user:
        return RedirectResponse(
            url=os.environ.get("FRONTEND_ORIGIN", "http://localhost:3100").rstrip("/") + "/doctor?calendar=error",
            status_code=302,
        )
    await db.user.update(
        where={"id": doctor_id},
        data={"googleRefreshToken": refresh_token, "googleCalendarId": None},
    )
    origin = (os.environ.get("FRONTEND_ORIGIN") or "http://localhost:3100").rstrip("/")
    return RedirectResponse(url=origin + "/doctor?calendar=connected", status_code=302)


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

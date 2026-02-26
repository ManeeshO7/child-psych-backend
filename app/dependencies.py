from typing import Annotated, Optional

import time

from fastapi import Cookie, Depends, Header, Response

from app.auth import (
    SESSION_MAX_AGE,
    create_session_payload,
    get_session_from_bearer,
    get_session_from_cookie,
)
from app.db import get_db

SessionUser = dict  # {"id": str, "email": str, "role": str, ...}


async def get_session(
    cookie: Annotated[Optional[str], Cookie(alias="telepsych_session")] = None,
    authorization: Annotated[Optional[str], Header()] = None,
    response: Response = None,
) -> Optional[SessionUser]:
    partial = get_session_from_cookie(cookie) or get_session_from_bearer(authorization)
    if not partial:
        return None
    db = await get_db()
    user = await db.user.find_unique(
        where={"id": partial["id"]},
    )
    if not user or user.role != partial["role"]:
        return None
    first = getattr(user, "first_name", None) or getattr(user, "firstName", "")
    last = getattr(user, "last_name", None) or getattr(user, "lastName", "")
    now = int(time.time())
    last_activity = int(partial.get("lat") or partial.get("iat") or now)
    # Sliding idle timeout: refresh session activity timestamp at most once per minute.
    if response is not None and (now - last_activity) >= 60:
        refreshed_token = create_session_payload(
            user_id=user.id,
            role=user.role,
            issued_at=int(partial.get("iat") or now),
            expires_at=now + SESSION_MAX_AGE,
            last_activity_at=now,
        )
        response.headers["X-Session-Token"] = refreshed_token

    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "firstName": first,
        "lastName": last,
        "iat": partial.get("iat"),
        "exp": partial.get("exp"),
        "lat": partial.get("lat"),
    }


async def require_doctor(
    session: Annotated[Optional[SessionUser], Depends(get_session)],
) -> SessionUser:
    if not session or session.get("role") != "doctor":
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    return session


async def require_patient(
    session: Annotated[Optional[SessionUser], Depends(get_session)],
) -> SessionUser:
    if not session or session.get("role") != "patient":
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    return session


async def require_patient_or_doctor(
    session: Annotated[Optional[SessionUser], Depends(get_session)],
) -> SessionUser:
    if not session or session.get("role") not in ("patient", "doctor"):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    return session

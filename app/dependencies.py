from typing import Annotated, Optional

from fastapi import Cookie, Depends, Header

from app.auth import get_session_from_bearer, get_session_from_cookie
from app.db import get_db

SessionUser = dict  # {"id": str, "email": str, "role": str, ...}


async def get_session(
    cookie: Annotated[Optional[str], Cookie(alias="telepsych_session")] = None,
    authorization: Annotated[Optional[str], Header()] = None,
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
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "firstName": first,
        "lastName": last,
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

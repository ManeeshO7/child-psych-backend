"""
Google Calendar API: create events with Google Meet for appointment bookings.
Uses doctor's stored OAuth refresh token.
"""
import logging
import os
from datetime import datetime
from uuid import uuid4

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

_LOG = logging.getLogger(__name__)

# Scopes needed for Calendar: create events and add Meet
SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/calendar.events"]


def get_credentials_from_refresh_token(refresh_token: str):
    """Build Credentials from stored refresh token (no access_token; will refresh on use)."""
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        _LOG.warning("GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set")
        return None
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )


def create_calendar_event_with_meet(
    *,
    refresh_token: str,
    calendar_id: str,
    start_utc: datetime,
    end_utc: datetime,
    summary: str,
    description: str = "",
    attendee_email: str | None = None,
) -> str | None:
    """
    Create a calendar event with a Google Meet link.
    Returns the Meet URL (entryPoint.uri for type "video") or None on failure.
    """
    creds = get_credentials_from_refresh_token(refresh_token)
    if not creds:
        return None
    try:
        service = build("calendar", "v3", credentials=creds)
        # RFC3339 for Calendar API
        start_rfc = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end_rfc = end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "summary": summary,
            "description": description or "",
            "start": {"dateTime": start_rfc, "timeZone": "UTC"},
            "end": {"dateTime": end_rfc, "timeZone": "UTC"},
            "conferenceData": {
                "createRequest": {
                    "requestId": uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]
        event = (
            service.events()
            .insert(
                calendarId=calendar_id or "primary",
                body=body,
                conferenceDataVersion=1,
                sendUpdates="none",
            )
            .execute()
        )
        # Extract Meet link from response
        entry_points = event.get("conferenceData", {}).get("entryPoints", [])
        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                uri = ep.get("uri")
                if uri:
                    _LOG.info("Created calendar event with Meet: %s", uri[:50])
                    return uri
        _LOG.warning("Calendar event created but no video entryPoint in response")
        return None
    except Exception as e:
        _LOG.exception("Calendar API create event failed: %s", e)
        return None

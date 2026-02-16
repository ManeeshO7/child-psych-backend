"""
Google Calendar API: create events with Google Meet for appointment bookings.
Uses doctor's stored OAuth refresh token.
"""
import logging
import os
from datetime import datetime
from uuid import uuid4

from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

_LOG = logging.getLogger(__name__)


class GoogleCalendarTokenExpiredError(Exception):
    """Raised when the stored refresh token is expired or revoked. Doctor must reconnect."""


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


def _extract_meet_link_and_id(event: dict) -> tuple[str | None, str | None]:
    """Extract meet link and event id from a Calendar API event response."""
    meet_link = None
    entry_points = event.get("conferenceData", {}).get("entryPoints", [])
    for ep in entry_points:
        if ep.get("entryPointType") == "video":
            uri = ep.get("uri")
            if uri:
                meet_link = uri
                break
    event_id = event.get("id")
    return (meet_link, event_id)


def create_calendar_event_with_meet(
    *,
    refresh_token: str,
    calendar_id: str,
    start_utc: datetime,
    end_utc: datetime,
    summary: str,
    description: str = "",
    attendee_email: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Create a calendar event with a Google Meet link.
    Returns (meet_link, event_id) or (None, None) on failure.
    """
    creds = get_credentials_from_refresh_token(refresh_token)
    if not creds:
        return (None, None)
    try:
        service = build("calendar", "v3", credentials=creds)
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
        meet_link, event_id = _extract_meet_link_and_id(event)
        if meet_link:
            _LOG.info("Created calendar event with Meet: %s", meet_link[:50])
        else:
            _LOG.warning("Calendar event created but no video entryPoint in response")
        return (meet_link, event_id)
    except RefreshError as e:
        _LOG.warning("Google Calendar token expired or revoked: %s", e)
        raise GoogleCalendarTokenExpiredError("Token expired or revoked") from e
    except Exception as e:
        _LOG.exception("Calendar API create event failed: %s", e)
        return (None, None)


def update_calendar_event(
    *,
    refresh_token: str,
    calendar_id: str,
    event_id: str,
    start_utc: datetime,
    end_utc: datetime,
    existing_meet_link: str | None = None,
) -> str | None:
    """
    Update an existing calendar event's start/end time.
    Meet link is preserved. Returns meet_link from response, or existing_meet_link
    if the API response omits it (common with patch), or None on failure.
    """
    creds = get_credentials_from_refresh_token(refresh_token)
    if not creds:
        return None
    try:
        service = build("calendar", "v3", credentials=creds)
        start_rfc = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end_rfc = end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "start": {"dateTime": start_rfc, "timeZone": "UTC"},
            "end": {"dateTime": end_rfc, "timeZone": "UTC"},
        }
        event = (
            service.events()
            .patch(
                calendarId=calendar_id or "primary",
                eventId=event_id,
                body=body,
                conferenceDataVersion=1,
                sendUpdates="none",
            )
            .execute()
        )
        meet_link, _ = _extract_meet_link_and_id(event)
        # Patch response often omits conferenceData; use existing link (same event, same Meet URL)
        if not meet_link and existing_meet_link:
            meet_link = existing_meet_link
        _LOG.info("Updated calendar event %s", event_id[:16] if event_id else "")
        return meet_link
    except RefreshError as e:
        _LOG.warning("Google Calendar token expired or revoked: %s", e)
        raise GoogleCalendarTokenExpiredError("Token expired or revoked") from e
    except Exception as e:
        _LOG.exception("Calendar API update event failed: %s", e)
        return None


def delete_calendar_event(
    *,
    refresh_token: str,
    calendar_id: str,
    event_id: str,
) -> bool:
    """Delete a calendar event. Returns True on success."""
    creds = get_credentials_from_refresh_token(refresh_token)
    if not creds:
        return False
    try:
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(
            calendarId=calendar_id or "primary",
            eventId=event_id,
            sendUpdates="none",
        ).execute()
        _LOG.info("Deleted calendar event %s", event_id[:16] if event_id else "")
        return True
    except RefreshError as e:
        _LOG.warning("Google Calendar token expired or revoked: %s", e)
        raise GoogleCalendarTokenExpiredError("Token expired or revoked") from e
    except Exception as e:
        _LOG.exception("Calendar API delete event failed: %s", e)
        return False

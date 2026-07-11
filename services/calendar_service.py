import hashlib
import base64
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(
    os.getenv("MARGOPLANNER_DATA_DIR", str(PROJECT_ROOT))
).expanduser()
TOKEN_PATH = DATA_DIR / "token.json"
CREDENTIALS_PATH = DATA_DIR / "credentials.json"
TIMEZONE_NAME = "Europe/Podgorica"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)


def _write_json_secret_from_base64(environment_name, destination):
    encoded = os.getenv(environment_name)
    if destination.exists() or not encoded:
        return
    decoded = base64.b64decode(encoded).decode("utf-8")
    json.loads(decoded)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(decoded, encoding="utf-8")


def initialize_calendar_files():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_secret_from_base64(
        "GOOGLE_CREDENTIALS_B64",
        CREDENTIALS_PATH,
    )
    _write_json_secret_from_base64("GOOGLE_TOKEN_B64", TOKEN_PATH)


def get_calendar_service():
    initialize_calendar_files()
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.getenv("RAILWAY_ENVIRONMENT"):
                raise RuntimeError(
                    "Google token нельзя авторизовать через браузер на сервере. "
                    "Передайте актуальный GOOGLE_TOKEN_B64."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH,
                SCOPES,
            )
            creds = flow.run_local_server(port=0)

        with TOKEN_PATH.open("w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def _compose_description(description="", links=None, contacts=None):
    parts = []
    if description:
        parts.append(description.strip())
    if links:
        parts.append("Ссылки:\n" + "\n".join(links))
    if contacts:
        parts.append("Контакты:\n" + "\n".join(contacts))
    return "\n\n".join(parts)


def _event_body(
    summary,
    start_time,
    end_time,
    event_id=None,
    description="",
    location="",
    links=None,
    contacts=None,
    attendees=None,
):
    event = {
        "summary": summary,
        "start": {"dateTime": start_time, "timeZone": TIMEZONE_NAME},
        "end": {"dateTime": end_time, "timeZone": TIMEZONE_NAME},
    }
    if event_id:
        event["id"] = event_id
    full_description = _compose_description(description, links, contacts)
    if full_description:
        event["description"] = full_description
    if location:
        event["location"] = location
    if attendees:
        event["attendees"] = [{"email": email} for email in attendees]
    return event


def create_event(
    summary,
    start_time,
    end_time,
    event_id=None,
    service=None,
    description="",
    location="",
    links=None,
    contacts=None,
    attendees=None,
):
    service = service or get_calendar_service()
    event = _event_body(
        summary,
        start_time,
        end_time,
        event_id,
        description,
        location,
        links,
        contacts,
        attendees,
    )
    request = {"calendarId": "primary", "body": event}
    if attendees:
        request["sendUpdates"] = "all"
    return (
        service.events()
        .insert(**request)
        .execute()
    )


def _batch_event_id(batch_id, position):
    # Google accepts base32hex characters; a lowercase SHA-256 hex digest is
    # valid and deterministic for safe retries.
    source = f"margoplanner:{batch_id}:{position}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def batch_event_ids(batch_id, event_count):
    return {_batch_event_id(batch_id, position) for position in range(event_count)}


def create_events(events, batch_id, service=None):
    """Create a retry-safe batch and return events in the original order."""
    if not batch_id:
        raise ValueError("Для пакетного создания нужен batch_id")

    service = service or get_calendar_service()
    created = []
    for position, event_data in enumerate(events):
        event_id = _batch_event_id(batch_id, position)
        try:
            result = create_event(
                summary=event_data["title"],
                start_time=event_data["start_time"],
                end_time=event_data["end_time"],
                event_id=event_id,
                service=service,
                description=event_data.get("description", ""),
                location=event_data.get("location", ""),
                links=event_data.get("links", []),
                contacts=event_data.get("contacts", []),
                attendees=event_data.get("attendees", []),
            )
        except HttpError as error:
            if error.resp.status != 409:
                raise
            result = (
                service.events()
                .get(calendarId="primary", eventId=event_id)
                .execute()
            )
        created.append(result)
    return created


def _parse_google_time(value):
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    return datetime.fromisoformat(value["date"]).replace(tzinfo=TIMEZONE)


def _overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


def find_conflicts(events, service=None, excluded_event_ids=None):
    if not events:
        return []

    service = service or get_calendar_service()
    proposed_start = min(
        datetime.fromisoformat(event["start_time"]) for event in events
    )
    proposed_end = max(
        datetime.fromisoformat(event["end_time"]) for event in events
    )
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=proposed_start.isoformat(),
            timeMax=proposed_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    conflicts = []
    excluded_event_ids = set(excluded_event_ids or [])
    for existing in result.get("items", []):
        if existing.get("id") in excluded_event_ids:
            continue
        if (
            existing.get("status") == "cancelled"
            or existing.get("transparency") == "transparent"
        ):
            continue
        existing_start = _parse_google_time(existing["start"])
        existing_end = _parse_google_time(existing["end"])
        for proposed in events:
            start = datetime.fromisoformat(proposed["start_time"])
            end = datetime.fromisoformat(proposed["end_time"])
            if _overlaps(start, end, existing_start, existing_end):
                conflicts.append(
                    {
                        "id": existing.get("id", ""),
                        "title": existing.get("summary", "Без названия"),
                        "start_time": existing_start.isoformat(),
                        "end_time": existing_end.isoformat(),
                    }
                )
                break
    return conflicts


def _normalize_google_event(event):
    return {
        "id": event["id"],
        "etag": event.get("etag", ""),
        "title": event.get("summary", "Без названия"),
        "start_time": _parse_google_time(event["start"]).isoformat(),
        "end_time": _parse_google_time(event["end"]).isoformat(),
        "htmlLink": event.get("htmlLink", ""),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "links": [],
        "contacts": [],
        "attendees": [
            attendee["email"]
            for attendee in event.get("attendees", [])
            if attendee.get("email")
        ],
    }


def search_events(search, service=None):
    service = service or get_calendar_service()
    now = datetime.now(TIMEZONE)
    time_min = search.get("time_min") or now.isoformat()
    time_max = search.get("time_max")
    request = {
        "calendarId": "primary",
        "timeMin": time_min,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 20,
    }
    if time_max:
        request["timeMax"] = time_max
    if search.get("text"):
        request["q"] = search["text"]
    result = service.events().list(**request).execute()
    return [
        _normalize_google_event(event)
        for event in result.get("items", [])
        if event.get("status") != "cancelled"
    ]


def get_event(event_id, service=None):
    service = service or get_calendar_service()
    event = (
        service.events()
        .get(calendarId="primary", eventId=event_id)
        .execute()
    )
    return _normalize_google_event(event)


def update_event(event_id, event_data, service=None):
    service = service or get_calendar_service()
    body = _event_body(
        event_data["title"],
        event_data["start_time"],
        event_data["end_time"],
        description=event_data.get("description", ""),
        location=event_data.get("location", ""),
        links=event_data.get("links", []),
        contacts=event_data.get("contacts", []),
        attendees=event_data.get("attendees", []),
    )
    request = {"calendarId": "primary", "eventId": event_id, "body": body}
    if event_data.get("attendees"):
        request["sendUpdates"] = "all"
    return (
        service.events()
        .patch(**request)
        .execute()
    )


def delete_event(event_id, service=None):
    service = service or get_calendar_service()
    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except HttpError as error:
        if error.resp.status not in {404, 410}:
            raise


def delete_events(event_ids, service=None):
    service = service or get_calendar_service()
    for event_id in event_ids:
        delete_event(event_id, service=service)

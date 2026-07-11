import unittest

from services.calendar_service import (
    _batch_event_id,
    batch_event_ids,
    create_events,
    find_conflicts,
    search_events,
    update_event,
    delete_event,
)


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeEventsResource:
    def __init__(self, listed=None):
        self.listed = listed or []
        self.inserted = []
        self.insert_requests = []
        self.patched = []
        self.deleted = []

    def insert(self, calendarId, body, sendUpdates=None):
        self.inserted.append(body)
        self.insert_requests.append(
            {"calendarId": calendarId, "body": body, "sendUpdates": sendUpdates}
        )
        return FakeRequest({"id": body["id"], "htmlLink": body["id"]})

    def list(self, **kwargs):
        return FakeRequest({"items": self.listed})

    def patch(self, calendarId, eventId, body, sendUpdates=None):
        self.patched.append((eventId, body))
        return FakeRequest({"id": eventId, "htmlLink": "updated"})

    def delete(self, calendarId, eventId):
        self.deleted.append(eventId)
        return FakeRequest(None)


class FakeService:
    def __init__(self, listed=None):
        self.resource = FakeEventsResource(listed)

    def events(self):
        return self.resource


class CalendarServiceTests(unittest.TestCase):
    def test_batch_ids_are_stable_and_unique(self):
        self.assertEqual(_batch_event_id("batch", 0), _batch_event_id("batch", 0))
        self.assertNotEqual(_batch_event_id("batch", 0), _batch_event_id("batch", 1))

    def test_batch_creation_assigns_deterministic_ids(self):
        service = FakeService()
        events = [
            {
                "title": "Дорога",
                "start_time": "2026-07-16T13:00:00+02:00",
                "end_time": "2026-07-16T15:00:00+02:00",
            },
            {
                "title": "Встреча",
                "start_time": "2026-07-16T15:00:00+02:00",
                "end_time": "2026-07-16T16:00:00+02:00",
            },
        ]
        create_events(events, "same-batch", service=service)
        ids = [item["id"] for item in service.resource.inserted]
        self.assertEqual(
            ids,
            [_batch_event_id("same-batch", 0), _batch_event_id("same-batch", 1)],
        )

    def test_links_contacts_location_and_explicit_attendees_reach_google(self):
        service = FakeService()
        events = [
            {
                "title": "Созвон с Дашей",
                "start_time": "2026-07-16T15:00:00+02:00",
                "end_time": "2026-07-16T16:00:00+02:00",
                "description": "Обсудить проект",
                "location": "Zoom",
                "links": ["https://zoom.us/example"],
                "contacts": ["@dasha"],
                "attendees": ["dasha@example.com"],
            }
        ]
        create_events(events, "details-batch", service=service)
        request = service.resource.insert_requests[0]
        body = request["body"]
        self.assertEqual(body["location"], "Zoom")
        self.assertIn("https://zoom.us/example", body["description"])
        self.assertIn("@dasha", body["description"])
        self.assertEqual(body["attendees"], [{"email": "dasha@example.com"}])
        self.assertEqual(request["sendUpdates"], "all")

    def test_conflicts_include_only_real_overlaps(self):
        service = FakeService(
            [
                {
                    "id": "busy",
                    "summary": "Занято",
                    "start": {"dateTime": "2026-07-16T14:30:00+02:00"},
                    "end": {"dateTime": "2026-07-16T15:30:00+02:00"},
                },
                {
                    "id": "free",
                    "summary": "Прозрачное событие",
                    "transparency": "transparent",
                    "start": {"dateTime": "2026-07-16T15:00:00+02:00"},
                    "end": {"dateTime": "2026-07-16T16:00:00+02:00"},
                },
            ]
        )
        events = [
            {
                "title": "Встреча",
                "start_time": "2026-07-16T15:00:00+02:00",
                "end_time": "2026-07-16T16:00:00+02:00",
            }
        ]
        conflicts = find_conflicts(events, service=service)
        self.assertEqual([item["id"] for item in conflicts], ["busy"])

    def test_current_batch_events_are_not_reported_as_conflicts(self):
        own_id = next(iter(batch_event_ids("retry-batch", 1)))
        service = FakeService(
            [
                {
                    "id": own_id,
                    "summary": "Уже созданная часть пакета",
                    "start": {"dateTime": "2026-07-16T15:00:00+02:00"},
                    "end": {"dateTime": "2026-07-16T16:00:00+02:00"},
                }
            ]
        )
        events = [
            {
                "title": "Встреча",
                "start_time": "2026-07-16T15:00:00+02:00",
                "end_time": "2026-07-16T16:00:00+02:00",
            }
        ]
        conflicts = find_conflicts(
            events,
            service=service,
            excluded_event_ids={own_id},
        )
        self.assertEqual(conflicts, [])

    def test_search_normalizes_google_events(self):
        service = FakeService(
            [
                {
                    "id": "event-1",
                    "etag": "version-1",
                    "summary": "Встреча с Дашей",
                    "start": {"dateTime": "2026-07-16T15:00:00+02:00"},
                    "end": {"dateTime": "2026-07-16T16:00:00+02:00"},
                }
            ]
        )
        result = search_events({"text": "Даша"}, service=service)
        self.assertEqual(result[0]["id"], "event-1")
        self.assertEqual(result[0]["title"], "Встреча с Дашей")

    def test_update_and_delete_use_the_selected_id(self):
        service = FakeService()
        event = {
            "title": "Новая встреча",
            "start_time": "2026-07-17T15:00:00+02:00",
            "end_time": "2026-07-17T16:00:00+02:00",
        }
        update_event("selected", event, service=service)
        delete_event("selected", service=service)
        self.assertEqual(service.resource.patched[0][0], "selected")
        self.assertEqual(service.resource.deleted, ["selected"])


if __name__ == "__main__":
    unittest.main()

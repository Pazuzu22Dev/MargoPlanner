from datetime import datetime, timezone

from services.calendar_service import get_calendar_service


def main():
    service = get_calendar_service()

    now = datetime.now(timezone.utc).isoformat()

    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = result.get("items", [])

    if not events:
        print("Календарь подключён. Ближайших событий нет.")
        return

    print("Календарь подключён. Ближайшие события:")

    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        title = event.get("summary", "Без названия")
        print(f"- {start}: {title}")


if __name__ == "__main__":
    main()
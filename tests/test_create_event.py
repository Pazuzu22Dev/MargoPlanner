from datetime import datetime, timedelta

from services.calendar_service import create_event


def main():
    start = datetime.now().replace(microsecond=0) + timedelta(minutes=10)
    end = start + timedelta(hours=1)

    event = create_event(
        summary="Тестовое событие Пинки",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
    )

    print("Событие создано:")
    print(event.get("htmlLink"))


if __name__ == "__main__":
    main()
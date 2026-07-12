from datetime import date, datetime
from zoneinfo import ZoneInfo


MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


def format_ru_date(value):
    parsed = value if isinstance(value, (date, datetime)) else date.fromisoformat(value)
    weekday = WEEKDAYS[parsed.weekday()].capitalize()
    return f"{weekday}, {parsed.day} {MONTHS[parsed.month - 1]}"


def format_calendar_action(data, timezone_name="Europe/Podgorica"):
    employee = str(data.get("employee", "")).strip()
    title = data.get("title", "Событие")
    lines = [f"📅 {title}"]
    if employee:
        lines.append(f"👤 {employee}")
    if data.get("all_day") or data.get("start_date"):
        lines.append(format_ru_date(data.get("start_date") or data["start_time"][:10]))
        return "\n".join(lines)
    timezone = ZoneInfo(timezone_name)
    start = datetime.fromisoformat(data["start_time"]).astimezone(timezone)
    end = datetime.fromisoformat(data["end_time"]).astimezone(timezone)
    lines.append(format_ru_date(start))
    lines.append(f"{start:%H:%M}–{end:%H:%M}")
    return "\n".join(lines)


def format_plan(plan, timezone_name="Europe/Podgorica"):
    labels = {
        "create_reminder": "🔔 Создать напоминание",
        "update_reminder": "✏️ Изменить напоминание",
        "delete_reminder": "🗑 Удалить напоминание",
        "delete_calendar_event": "🗑 Удалить событие",
    }
    blocks = []
    for number, item in enumerate(plan["actions"], start=1):
        action = item["action"]
        data = item["data"]
        if action in {"create_calendar_event", "update_calendar_event"}:
            body = format_calendar_action(data, timezone_name)
        else:
            title = data.get("text") or data.get("title") or str(data.get("id", ""))
            body = f"{labels.get(action, action)}: {title}"
        blocks.append(f"{number}. {body}")
    return "\n\n".join(blocks)

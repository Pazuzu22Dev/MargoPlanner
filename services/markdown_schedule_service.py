import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TIME_RANGE = re.compile(
    r"(?P<start>\d{1,2}[:.]\d{2})\s*[–—-]\s*(?P<end>\d{1,2}[:.]\d{2})"
)
NUMERIC_DATE = re.compile(r"\b(?P<day>\d{1,2})[./](?P<month>\d{1,2})(?:[./](?P<year>\d{2,4}))?\b")
TEXT_DATE = re.compile(
    r"\b(?P<day>\d{1,2})\s+(?P<month>январ[ья]|феврал[ья]|марта?|апрел[ья]|мая|июн[ья]|июл[ья]|августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])\b",
    re.IGNORECASE,
)
MONTH_NUMBERS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def is_markdown_table(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    pipe_lines = [line for line in lines if line.count("|") >= 2]
    return len(pipe_lines) >= 3 and any(
        re.fullmatch(r"\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?", line)
        for line in pipe_lines
    )


def looks_like_schedule(text):
    normalized = text.casefold()
    return (
        "марго" in normalized
        and bool(TIME_RANGE.search(text))
        and bool(NUMERIC_DATE.search(text) or TEXT_DATE.search(text))
        and any(word in normalized for word in ("смен", "день", "дата"))
    )


def _cells(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _parse_date(text, now):
    match = NUMERIC_DATE.search(text)
    if match:
        year = int(match.group("year") or now.year)
        if year < 100:
            year += 2000
        return datetime(year, int(match.group("month")), int(match.group("day")))
    match = TEXT_DATE.search(text)
    if not match:
        return None
    month_word = match.group("month").casefold()
    month = next(
        number for stem, number in MONTH_NUMBERS.items() if month_word.startswith(stem)
    )
    return datetime(now.year, month, int(match.group("day")))


def parse_markdown_shifts(text, employee="Марго", timezone_name="Europe/Podgorica"):
    markdown_table = is_markdown_table(text)
    if not markdown_table and not looks_like_schedule(text):
        return None
    if not markdown_table:
        return _parse_plain_schedule(text, employee, timezone_name)
    lines = [line.strip() for line in text.splitlines() if line.strip() and "|" in line]
    separator = next(
        index for index, line in enumerate(lines)
        if re.fullmatch(r"\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?", line)
    )
    headers = _cells(lines[separator - 1])
    ranges = {}
    for index, header in enumerate(headers):
        match = TIME_RANGE.search(header)
        if match:
            ranges[index] = (
                match.group("start").replace(".", ":"),
                match.group("end").replace(".", ":"),
            )
    if not ranges:
        return {
            "actions": [],
            "clarification_question": "Я вижу таблицу, но не нашла интервалы смен в заголовках столбцов.",
            "notes": [],
        }
    timezone = ZoneInfo(timezone_name)
    now = datetime.now(timezone)
    actions = []
    incomplete = []
    for row_number, line in enumerate(lines[separator + 1:], start=1):
        cells = _cells(line)
        row_text = " ".join(cells)
        row_date = _parse_date(row_text, now)
        if not row_date:
            continue
        for column, (start_text, end_text) in ranges.items():
            if column >= len(cells):
                incomplete.append(row_number)
                continue
            value = cells[column].strip()
            if not value or value.casefold() == "выходной":
                continue
            if employee.casefold() not in value.casefold():
                continue
            start_hour, start_minute = map(int, start_text.split(":"))
            end_hour, end_minute = map(int, end_text.split(":"))
            start = row_date.replace(hour=start_hour, minute=start_minute, tzinfo=timezone)
            end = row_date.replace(hour=end_hour, minute=end_minute, tzinfo=timezone)
            if end <= start:
                end += timedelta(days=1)
            actions.append({
                "action": "create_calendar_event",
                "data": {
                    "title": "Рабочая смена",
                    "employee": employee,
                    "row_type": "Смена",
                    "source_row": row_number,
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                },
            })
    notes = []
    if incomplete:
        notes.append("Часть строк таблицы выглядит обрезанной; проверь найденные смены.")
    if not actions:
        return {
            "actions": [],
            "clarification_question": "Я прочитала таблицу, но не нашла в ней смены Марго.",
            "notes": notes,
        }
    return {"actions": actions, "clarification_question": "", "notes": notes}


def _parse_plain_schedule(text, employee, timezone_name):
    ranges = [
        (
            match.group("start").replace(".", ":"),
            match.group("end").replace(".", ":"),
        )
        for match in TIME_RANGE.finditer(text)
    ]
    # The rendered Telegram table used by Valera currently has one shift
    # interval in its header. Multiple intervals require preserved columns.
    unique_ranges = list(dict.fromkeys(ranges))
    if len(unique_ranges) != 1:
        return {
            "actions": [],
            "clarification_question": (
                "Я вижу несколько интервалов смен, но Telegram не сохранил "
                "границы столбцов. Перешли таблицу как Markdown-текст или файл."
            ),
            "notes": [],
        }
    start_text, end_text = unique_ranges[0]
    timezone = ZoneInfo(timezone_name)
    now = datetime.now(timezone)
    actions = []
    for row_number, line in enumerate(text.splitlines(), start=1):
        row_date = _parse_date(line, now)
        if not row_date or employee.casefold() not in line.casefold():
            continue
        if "выходной" in line.casefold():
            continue
        start_hour, start_minute = map(int, start_text.split(":"))
        end_hour, end_minute = map(int, end_text.split(":"))
        start = row_date.replace(hour=start_hour, minute=start_minute, tzinfo=timezone)
        end = row_date.replace(hour=end_hour, minute=end_minute, tzinfo=timezone)
        if end <= start:
            end += timedelta(days=1)
        actions.append({
            "action": "create_calendar_event",
            "data": {
                "title": "Рабочая смена",
                "employee": employee,
                "row_type": "Смена",
                "source_row": row_number,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            },
        })
    if not actions:
        return {
            "actions": [],
            "clarification_question": "Я вижу расписание, но не смогла выделить строки Марго.",
            "notes": [],
        }
    return {"actions": actions, "clarification_question": "", "notes": []}

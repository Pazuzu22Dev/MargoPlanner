import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

from services.calendar_service import (
    CalendarAuthorizationError,
    batch_event_ids,
    create_events,
    delete_event,
    delete_events as delete_calendar_events,
    find_conflicts,
    get_event,
    search_events,
    update_event,
)
from services.action_history_service import ActionHistoryStore
from services.chat_service import get_chat_reply
from services.conversation_service import (
    ConversationState,
    add_user_message,
    apply_intent,
    clear_conversation,
    get_conversation,
    new_conversation,
    save_conversation,
)
from services.intent_service import detect_intent
from services.memory_service import MemoryStore, infer_stable_memories
from services.voice_service import VoiceQuotaError, transcribe_voice
from services.reminder_service import ReminderStore
from services.action_executor import (
    execute_batch,
    execute_plan,
    format_plan,
    summarize_plan_execution,
)
from services.batch_service import (
    analyze_batch,
    batch_followup_response,
    batch_counts,
    format_batch_report,
    format_conflict,
    format_duplicate,
    format_execution_report,
    find_retry_action,
)
from services.extraction_service import extract_content
from services.input_service import (
    InputPayload,
    detect_message_input,
    extract_rich_message_text,
    get_message_attachment,
    is_structured_telegram_text,
    normalize_telegram_message,
)
from services.input_dedup_service import InputDedupStore
from services.markdown_schedule_service import (
    is_markdown_table,
    looks_like_schedule,
    parse_markdown_shifts,
)
from services.planner_service import build_plan


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.getenv("MARGOPLANNER_DATA_DIR", str(PROJECT_ROOT / "storage"))
).expanduser()
PERSISTENCE_PATH = DATA_DIR / "telegram_state.pickle"
MEMORY_PATH = DATA_DIR / "memory.sqlite"
ACTION_HISTORY_PATH = DATA_DIR / "actions.sqlite"
REMINDER_PATH = DATA_DIR / "reminders.sqlite"
INPUT_DEDUP_PATH = DATA_DIR / "processed_inputs.sqlite"
memory_store = MemoryStore(MEMORY_PATH)
memory_store.apply_updates([
    {
        "operation": "set",
        "category": "preference",
        "key": "профессия Марго",
        "value": (
            "Марго — Senior UI Artist / Lead UI Designer, художник и "
            "дизайнер игровых интерфейсов."
        ),
    },
    {
        "operation": "set",
        "category": "preference",
        "key": "сменный рабочий график",
        "value": (
            "Марго работает по сменному графику. Конкретные даты и время "
            "смен нужно брать из актуального сообщения или Google Calendar."
        ),
    },
])
action_history_store = ActionHistoryStore(ACTION_HISTORY_PATH)
reminder_store = ReminderStore(REMINDER_PATH)
input_dedup_store = InputDedupStore(INPUT_DEDUP_PATH)
logger = logging.getLogger(__name__)
LOCAL_TIMEZONE = ZoneInfo("Europe/Podgorica")
MORNING_DIGEST_HOUR = int(os.getenv("MORNING_DIGEST_HOUR", "8"))
if not 0 <= MORNING_DIGEST_HOUR <= 23:
    raise RuntimeError("MORNING_DIGEST_HOUR должен быть от 0 до 23")

if not BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в .env")


def parse_allowed_user_id(raw_value):
    if not raw_value:
        return None
    try:
        user_id = int(raw_value)
    except ValueError as error:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_ID должен быть целым числом"
        ) from error
    if user_id <= 0:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID должен быть положительным")
    return user_id


ALLOWED_USER_ID = parse_allowed_user_id(
    os.getenv("TELEGRAM_ALLOWED_USER_ID")
)


YES_ANSWERS = {"да", "ага", "создавай", "подтверждаю", "ок", "окей"}
NO_ANSWERS = {"нет", "отмена", "не надо", "отменить"}
UNDO_REQUESTS = {
    "отмени последнее действие",
    "отменить последнее действие",
    "верни последнее действие",
    "откати последнее действие",
    "отмени последнее",
}
MAX_VOICE_DURATION_SECONDS = 10 * 60
MAX_VOICE_SIZE_BYTES = 15 * 1024 * 1024


def format_reminder(reminder):
    remind_at = datetime.fromisoformat(reminder["remind_at"])
    return (
        f"🔔 {reminder['text']}\n"
        f"🕒 {remind_at.strftime('%d.%m.%Y в %H:%M')}"
    )


def format_reminder_list(reminders):
    lines = []
    for number, reminder in enumerate(reminders, start=1):
        remind_at = datetime.fromisoformat(reminder["remind_at"]).astimezone(
            LOCAL_TIMEZONE
        )
        lines.append(f"{number}. {remind_at.strftime('%H:%M')} — {reminder['text']}")
    return "\n".join(lines)


def format_memories(memories):
    category_labels = {
        "person": "👤 Люди",
        "place": "📍 Места",
        "project": "🎨 Проекты",
        "preference": "💡 Предпочтения и привычки",
    }
    sections = []
    for category, label in category_labels.items():
        items = [item for item in memories if item["category"] == category]
        if items:
            sections.append(
                label + ":\n" + "\n".join(
                    f"• {item['value']}" for item in items
                )
            )
    return "\n\n".join(sections)


def format_memory_updates(updates):
    return "\n".join(f"• {item['value'] or item['key']}" for item in updates)


def reminder_followup_keyboard(reminder_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Сделала", callback_data=f"followup:done:{reminder_id}"
        ),
        InlineKeyboardButton(
            "🙈 Забыла", callback_data=f"followup:forgot:{reminder_id}"
        ),
    ]])


def reminder_repeat_keyboard(reminder_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Да", callback_data=f"followup:snooze:{reminder_id}"
        ),
        InlineKeyboardButton(
            "❌ Нет", callback_data=f"followup:close:{reminder_id}"
        ),
    ]])


def format_daily_digest(events, reminders, local_now):
    weekdays = (
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    )
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    sections = [
        "☀️ Доброе утро!",
        f"План на {weekdays[local_now.weekday()]}, "
        f"{local_now.day} {months[local_now.month - 1]}:",
    ]
    if events:
        lines = []
        for index, event in enumerate(events, start=1):
            start = datetime.fromisoformat(event["start_time"]).astimezone(
                LOCAL_TIMEZONE
            )
            end = datetime.fromisoformat(event["end_time"]).astimezone(
                LOCAL_TIMEZONE
            )
            lines.append(
                f"{index}. {start.strftime('%H:%M')}–{end.strftime('%H:%M')} "
                f"— {event['title']}"
            )
        sections.append("📅 Календарь:\n" + "\n".join(lines))
    else:
        sections.append("📅 В календаре на сегодня ничего нет.")
    if reminders:
        lines = []
        for index, reminder in enumerate(reminders, start=1):
            remind_at = datetime.fromisoformat(reminder["remind_at"]).astimezone(
                LOCAL_TIMEZONE
            )
            lines.append(
                f"{index}. {remind_at.strftime('%H:%M')} — {reminder['text']}"
            )
        sections.append("🔔 Напоминания:\n" + "\n".join(lines))
    else:
        sections.append("🔔 Напоминаний на сегодня нет.")
    return "\n\n".join(sections)


def confirmation_keyboard():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Нет", callback_data="confirm:no"),
        ]]
    )


def selection_keyboard(items, kind, destructive=True):
    icon = "🗑" if destructive else "👉"
    rows = []
    for index, item in enumerate(items):
        title = item.get("title") or item.get("text") or "Без названия"
        if kind == "event" and item.get("start_time"):
            starts_at = datetime.fromisoformat(item["start_time"]).astimezone(
                LOCAL_TIMEZONE
            )
            title = f"{starts_at.strftime('%d.%m %H:%M')} · {title}"
        rows.append([
            InlineKeyboardButton(
                f"{icon} {title[:48]}",
                callback_data=f"select:{kind}:{index}",
            )
        ])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def multi_event_selection_keyboard(items, selected_indexes=None):
    selected = set(selected_indexes or [])
    rows = []
    for index, item in enumerate(items):
        starts_at = datetime.fromisoformat(item["start_time"]).astimezone(
            LOCAL_TIMEZONE
        )
        mark = "✅" if index in selected else "⬜️"
        label = f"{mark} {starts_at.strftime('%H:%M')} · {item['title']}"
        rows.append([InlineKeyboardButton(
            label[:60], callback_data=f"multiselect:event:{index}"
        )])
    rows.append([InlineKeyboardButton(
        f"➡️ Готово ({len(selected)})", callback_data="multiselect:done"
    )])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def build_group_update_events(candidates, selected_indexes, proposed_event):
    """Move a related event chain while preserving durations and gaps."""
    selected = [candidates[index] for index in selected_indexes]
    if not selected:
        return [], []

    def is_travel(event):
        title = event.get("title", "").casefold().replace("ё", "е")
        return any(word in title for word in ("дорога", "путь", "проезд"))

    # The time mentioned by Margo normally belongs to the main appointment,
    # not to its travel blocks. Prefer the non-travel item as the anchor.
    anchor = next((event for event in selected if not is_travel(event)), selected[0])
    anchor_start = datetime.fromisoformat(anchor["start_time"])
    destination_start = datetime.fromisoformat(proposed_event["start_time"])
    delta = destination_start - anchor_start

    updated = []
    for source in selected:
        start = datetime.fromisoformat(source["start_time"]) + delta
        end = datetime.fromisoformat(source["end_time"]) + delta
        updated.append({
            "title": source["title"],
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "description": source.get("description", ""),
            "location": source.get("location", ""),
            "links": source.get("links", []),
            "contacts": source.get("contacts", []),
            "attendees": source.get("attendees", []),
        })
    return selected, updated


def _relative_calendar_range(user_text):
    """Return a local day range explicitly referenced by the user."""
    normalized = user_text.casefold().replace("ё", "е")
    offset = None
    if "послезавтра" in normalized:
        offset = 2
    elif "завтра" in normalized:
        offset = 1
    elif "сегодня" in normalized:
        offset = 0
    if offset is None:
        return None
    start = datetime.now(LOCAL_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=offset)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def search_calendar_candidates(search, user_text):
    """Search narrowly first, then fall back to all events on the named day."""
    effective = dict(search or {})
    relative_range = _relative_calendar_range(user_text)
    if relative_range:
        # The user's explicit relative date is more trustworthy than an
        # omitted range from the model. Existing explicit bounds are kept.
        if not effective.get("time_min"):
            effective["time_min"] = relative_range[0]
        if not effective.get("time_max"):
            effective["time_max"] = relative_range[1]

    candidates = search_events(effective)
    if candidates or not effective.get("text"):
        return candidates

    # Google Calendar's q filter is lexical. Natural phrases such as
    # "отмена по бару и Лизе" can fail to match a differently-worded title.
    # If a day/range is known, show that day's events and let Margo choose.
    if effective.get("time_min") and effective.get("time_max"):
        broad_search = dict(effective)
        broad_search["text"] = ""
        return search_events(broad_search)
    return candidates


def _generic_calendar_delete_intent(user_text):
    """Build a deterministic search for an underspecified delete request."""
    normalized = user_text.casefold().replace("ё", "е")
    asks_to_delete = any(stem in normalized for stem in ("удал", "отмен"))
    mentions_event = any(
        stem in normalized for stem in ("событ", "встреч", "запис")
    )
    if not asks_to_delete or not mentions_event or "напомин" in normalized:
        return None
    relative_range = _relative_calendar_range(user_text)
    if relative_range:
        time_min, time_max = relative_range
    else:
        start = datetime.now(LOCAL_TIMEZONE)
        time_min = start.isoformat()
        time_max = (start + timedelta(days=14)).isoformat()
    return {
        "action": "delete_event",
        "clarification_question": "",
        "reason": "Выбрать календарное событие для удаления",
        "target_event_id": "",
        "search": {"text": "", "time_min": time_min, "time_max": time_max},
        "events": [],
        "memory_updates": [],
        "reminder": {},
        "target_reminder_ids": [],
    }


def parse_explicit_reminder_request(user_text, local_now=None):
    """Separate delivery time from a later time mentioned in reminder text."""
    match = re.search(
        r"\bнапомни(?:\s+мне)?[\s,]*"
        r"(?:(сегодня|завтра|послезавтра)\s+)?"
        r"в\s+(\d{1,2})(?::(\d{2}))?"
        r"(?:\s*час(?:а|ов)?)?\b(.*)",
        user_text,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?:(сегодня|завтра|послезавтра)\s+)?"
            r"\bв\s+(\d{1,2})(?::(\d{2}))?"
            r"(?:\s*час(?:а|ов)?)?[\s,]*"
            r"напомни(?:\s+мне)?\b(.*)",
            user_text,
            flags=re.IGNORECASE,
        )
    if not match:
        return None
    day_word, hour_text, minute_text, remainder = match.groups()
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if hour > 23 or minute > 59:
        return None
    now = (local_now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    day_offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}.get(
        (day_word or "").casefold()
    )
    remind_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day_offset is not None:
        remind_at += timedelta(days=day_offset)
    elif remind_at <= now:
        remind_at += timedelta(days=1)

    text = remainder.strip(" ,.—-:")
    text = re.sub(
        r"^(?:о\s+том\s*,?\s*что|что)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if not text:
        return None
    text = text[0].upper() + text[1:]
    return {"text": text, "remind_at": remind_at.isoformat()}


def parse_shift_schedule_request(user_text, local_now=None):
    """Parse compact weekly shift messages without relying on Gemini."""
    normalized = str(user_text).casefold().replace("ё", "е")
    if "смен" not in normalized:
        return None
    weekday_patterns = {
        0: r"\b(?:пн|понедельник)\b",
        1: r"\b(?:вт|вторник)\b",
        2: r"\b(?:ср|среда|среду)\b",
        3: r"\b(?:чт|четверг)\b",
        4: r"\b(?:пт|пятница|пятницу)\b",
        5: r"\b(?:сб|суббота|субботу)\b",
        6: r"\b(?:вс|воскресенье)\b",
    }
    if not any(re.search(pattern, normalized) for pattern in weekday_patterns.values()):
        return None

    slots = {}
    for slot_number, slot_pattern in (
        (1, r"(?:первая|1)\s*(?:смена)?"),
        (2, r"(?:вторая|2)\s*(?:смена)?"),
    ):
        match = re.search(
            slot_pattern
            + r"\s*(\d{1,2})(?::(\d{2}))?\s*[-–]\s*"
            r"(\d{1,2})(?::(\d{2}))?",
            normalized,
        )
        if match:
            slots[slot_number] = (
                int(match.group(1)), int(match.group(2) or 0),
                int(match.group(3)), int(match.group(4) or 0),
            )
    slots.setdefault(1, (8, 0, 15, 0))
    slots.setdefault(2, (15, 0, 22, 0))

    now = (local_now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start -= timedelta(days=week_start.weekday())
    if "следующ" in normalized and "недел" in normalized:
        week_start += timedelta(days=7)

    events = []
    for clause in re.split(r"[,.;\n]+", normalized):
        weekdays = [
            number for number, pattern in weekday_patterns.items()
            if re.search(pattern, clause)
        ]
        if not weekdays or "выходн" in clause:
            continue
        slot_number = None
        if re.search(r"\b(?:первая|1)\s*(?:смена)?\b", clause):
            slot_number = 1
        elif re.search(r"\b(?:вторая|2)\s*(?:смена)?\b", clause):
            slot_number = 2
        if slot_number is None:
            continue
        start_hour, start_minute, end_hour, end_minute = slots[slot_number]
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
            return None
        for weekday in weekdays:
            day = week_start + timedelta(days=weekday)
            start = day.replace(hour=start_hour, minute=start_minute)
            end = day.replace(hour=end_hour, minute=end_minute)
            if end <= start:
                end += timedelta(days=1)
            events.append({
                "title": "Рабочая смена",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "description": "",
                "location": "",
                "links": [],
                "contacts": [],
                "attendees": [],
            })
    unique = {
        (event["start_time"], event["end_time"]): event for event in events
    }
    return list(unique.values()) or None


def is_multi_action_request(user_text):
    """Return True when one message asks Pinky to perform several actions."""
    normalized = " ".join(str(user_text).casefold().replace("ё", "е").split())
    if not re.search(r"\b(?:и|а еще|также|плюс)\b", normalized):
        return False

    action_verbs = re.findall(
        r"\b(?:добавь|добавить|создай|создать|сделай|сделать|поставь|"
        r"поставить|напомни|напомнить|перенеси|перенести|измени|изменить|"
        r"удали|удалить|отмени|отменить)\b",
        normalized,
    )
    has_calendar_object = bool(
        re.search(r"\b(?:событи\w*|встреч\w*|календар\w*)\b", normalized)
    )
    has_reminder_object = bool(re.search(r"\bнапомин\w*\b", normalized))
    return len(action_verbs) >= 2 or (
        bool(action_verbs) and has_calendar_object and has_reminder_object
    )


def parse_calendar_list_request(user_text, local_now=None):
    """Build a real calendar query for natural requests such as Monday plans."""
    normalized = " ".join(str(user_text).casefold().replace("ё", "е").split())
    if any(word in normalized for word in (
        "добавь", "добавить", "создай", "создать", "перенеси", "перенести",
        "удали", "удалить", "отмени", "отменить", "напомни", "напомнить",
    )):
        return None
    asks_to_look = any(marker in normalized for marker in (
        "что у меня", "что стоит", "что запланировано", "какие событ",
        "какие встреч", "какие созвон", "покажи календар", "проверь календар",
        "загляни в календар", "расписание на", "планы на",
    ))
    if not asks_to_look:
        return None

    now = (local_now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    relative_days = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    offset = next(
        (value for word, value in relative_days.items() if word in normalized),
        None,
    )
    if offset is None:
        weekdays = {
            "понедельник": 0,
            "вторник": 1,
            "сред": 2,
            "четверг": 3,
            "пятниц": 4,
            "суббот": 5,
            "воскресень": 6,
        }
        target_weekday = next(
            (number for stem, number in weekdays.items() if stem in normalized),
            None,
        )
        if target_weekday is None:
            return None
        offset = (target_weekday - day_start.weekday()) % 7
    day_start += timedelta(days=offset)
    day_end = day_start + timedelta(days=1)
    return {
        "action": "show_calendar",
        "clarification_question": "",
        "reason": "Показать реальные события из Google Calendar",
        "events": [],
        "memory_updates": [],
        "search": {
            "text": "",
            "time_min": day_start.isoformat(),
            "time_max": day_end.isoformat(),
        },
    }


def reminder_actions_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить", callback_data="reminders:add")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="reminders:delete")],
        [InlineKeyboardButton("🧹 Очистить все", callback_data="reminders:clear")],
    ])


def plan_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Добавить всё", callback_data="plan:execute")],
        [InlineKeyboardButton("✏️ Исправить", callback_data="plan:edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def batch_actions_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ Создать только без конфликтов",
            callback_data="batchall:safe",
        )],
        [InlineKeyboardButton("⚠️ Создать всё", callback_data="batchall:all")],
        [InlineKeyboardButton("🧹 Удалить дубли", callback_data="batchall:dedupe")],
        [InlineKeyboardButton(
            "🔎 Разобрать конфликты",
            callback_data="batchall:review",
        )],
        [InlineKeyboardButton("✏️ Исправить", callback_data="plan:edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def conflict_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Оставить существующее", callback_data="batch:keep")],
        [InlineKeyboardButton("🔁 Заменить новым", callback_data="batch:replace")],
        [InlineKeyboardButton("➕ Оставить оба", callback_data="batch:both")],
        [InlineKeyboardButton("✏️ Изменить новое", callback_data="batch:edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="batch:cancel")],
    ])


def duplicate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♻️ Пропустить дубль", callback_data="batch:keep")],
        [InlineKeyboardButton("🔁 Заменить новым", callback_data="batch:replace")],
        [InlineKeyboardButton("➕ Оставить оба", callback_data="batch:both")],
        [InlineKeyboardButton("✏️ Изменить новое", callback_data="batch:edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="batch:cancel")],
    ])


async def reminder_dispatcher(application):
    reminder_store.recover_interrupted()
    while True:
        for reminder in await asyncio.to_thread(reminder_store.claim_due):
            try:
                await application.bot.send_message(
                    chat_id=reminder["user_id"],
                    text="🔔 Напоминаю: " + reminder["text"],
                )
            except Exception:
                reminder_store.release(reminder["id"])
                logger.exception("Не удалось отправить напоминание id=%s", reminder["id"])
            else:
                await asyncio.to_thread(reminder_store.mark_sent, reminder["id"])
        for reminder in await asyncio.to_thread(
            reminder_store.claim_due_followups
        ):
            try:
                await application.bot.send_message(
                    chat_id=reminder["user_id"],
                    text=(
                        "Прошёл час после напоминания:\n\n"
                        f"🔔 {reminder['text']}\n\nТы сделала или забыла?"
                    ),
                    reply_markup=reminder_followup_keyboard(reminder["id"]),
                )
            except Exception:
                await asyncio.to_thread(
                    reminder_store.release_followup, reminder["id"]
                )
                logger.exception(
                    "Не удалось отправить follow-up напоминания id=%s",
                    reminder["id"],
                )
            else:
                await asyncio.to_thread(
                    reminder_store.mark_followup_sent, reminder["id"]
                )
        try:
            await send_morning_digest_if_due(application)
        except Exception:
            logger.exception("Не удалось отправить утреннюю сводку")
        await asyncio.sleep(20)


async def send_morning_digest_if_due(application, local_now=None):
    if ALLOWED_USER_ID is None:
        return False
    now = (local_now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    if now.hour != MORNING_DIGEST_HOUR:
        return False
    local_date = now.date().isoformat()
    already_sent = await asyncio.to_thread(
        reminder_store.was_digest_sent, ALLOWED_USER_ID, local_date
    )
    if already_sent:
        return False
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    events, reminders = await asyncio.gather(
        asyncio.to_thread(search_events, {
            "text": "",
            "time_min": day_start.isoformat(),
            "time_max": day_end.isoformat(),
        }),
        asyncio.to_thread(
            reminder_store.list_scheduled,
            ALLOWED_USER_ID,
            day_start.isoformat(),
            day_end.isoformat(),
        ),
    )
    await application.bot.send_message(
        chat_id=ALLOWED_USER_ID,
        text=format_daily_digest(events, reminders, now),
    )
    await asyncio.to_thread(
        reminder_store.mark_digest_sent, ALLOWED_USER_ID, local_date
    )
    return True


async def start_background_tasks(application):
    application.create_task(
        reminder_dispatcher(application),
        name="reminder-dispatcher",
    )


def _conflict_signature(conflicts):
    return sorted(
        (item.get("id"), item.get("start_time"), item.get("end_time"))
        for item in conflicts
    )


def format_conflicts(conflicts):
    if not conflicts:
        return ""
    lines = ["⚠️ В календаре уже занято:"]
    for conflict in conflicts:
        start = datetime.fromisoformat(conflict["start_time"])
        end = datetime.fromisoformat(conflict["end_time"])
        lines.append(
            f"• {conflict['title']} — "
            f"{start.strftime('%d.%m %H:%M')}–{end.strftime('%H:%M')}"
        )
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize_update(update):
        return
    await update.message.reply_text(
        "Да, моя госпожа. 👋 Я MargoPlanner и готов к работе."
    )


async def authorize_update(update):
    user = update.effective_user
    if user is None:
        return False
    if ALLOWED_USER_ID is None:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"Режим безопасной настройки. Ваш Telegram ID: {user.id}\n\n"
                "Добавьте в .env строку:\n"
                f"TELEGRAM_ALLOWED_USER_ID={user.id}\n\n"
                "Затем перезапустите бота. До этого календарь и память "
                "полностью отключены."
            )
        return False
    if user.id != ALLOWED_USER_ID:
        logger.warning("Отклонён Telegram user_id=%s", user.id)
        return False
    return True


def format_events(events: list[dict]) -> str:
    lines = []

    for event in events:
        start = datetime.fromisoformat(event["start_time"])
        end = datetime.fromisoformat(event["end_time"])

        lines.append(
            f"📌 {event['title']}\n"
            f"📅 {start.strftime('%d.%m.%Y')}\n"
            f"🕒 {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        )
        details = []
        if event.get("location"):
            details.append(f"📍 {event['location']}")
        for link in event.get("links", []):
            details.append(f"🔗 {link}")
        for contact in event.get("contacts", []):
            details.append(f"👤 {contact}")
        if event.get("description"):
            details.append(f"📝 {event['description']}")
        for attendee in event.get("attendees", []):
            details.append(f"📨 Пригласить: {attendee}")
        if details:
            lines[-1] += "\n" + "\n".join(details)

    return "\n\n".join(lines)


def format_candidates(candidates):
    lines = []
    for number, event in enumerate(candidates, start=1):
        start = datetime.fromisoformat(event["start_time"])
        lines.append(
            f"{number}. {event['title']} — "
            f"{start.strftime('%d.%m.%Y в %H:%M')}"
        )
    return "\n".join(lines)


def _event_changed(saved, current):
    if saved.get("etag") and current.get("etag"):
        return saved["etag"] != current["etag"]
    fields = ("title", "start_time", "end_time")
    return any(saved.get(field) != current.get(field) for field in fields)


def parse_candidate_selection(user_text, candidate_count):
    normalized = user_text.casefold().strip()
    if normalized in {"все", "всё", "оба", "обе", "все события", "удали все"}:
        return list(range(candidate_count))
    if not re.fullmatch(
        r"(?:удали\s+)?(?:номер(?:а)?\s+)?\d+(?:\s*(?:,|и)\s*\d+)*",
        normalized,
    ):
        return None
    indexes = [int(value) - 1 for value in re.findall(r"\d+", normalized)]
    if not indexes or any(index < 0 or index >= candidate_count for index in indexes):
        return None
    return list(dict.fromkeys(indexes))


def format_undo_action(action):
    action_type = action["action_type"]
    payload = action["payload"]
    if action_type == "create_events":
        return "Удалить созданные события:\n\n" + format_events(payload["events"])
    if action_type == "update_event":
        return (
            "Вернуть событие к прежнему состоянию:\n\n"
            + format_events([payload["before"]])
        )
    if action_type == "update_events":
        return (
            "Вернуть связанные события к прежнему состоянию:\n\n"
            + format_events([item["before"] for item in payload["updates"]])
        )
    return "Восстановить удалённые события:\n\n" + format_events(
        payload["events"]
    )


def undo_calendar_action(action):
    action_type = action["action_type"]
    payload = action["payload"]
    if action_type == "create_events":
        delete_calendar_events([event["id"] for event in payload["events"]])
        return "Удалила события, созданные последним действием."
    if action_type == "update_event":
        update_event(payload["event_id"], payload["before"])
        return "Вернула событие к прежнему состоянию."
    if action_type == "update_events":
        for item in payload["updates"]:
            update_event(item["event_id"], item["before"])
        return f"Вернула связанных событий: {len(payload['updates'])}."
    create_events(
        payload["events"],
        f"undo{action['id']}{uuid4().hex}",
    )
    return f"Восстановила событий: {len(payload['events'])}."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize_update(update):
        return
    normalized = normalize_telegram_message(update.message)
    replied = getattr(update.message, "reply_to_message", None)
    external_reply = getattr(update.message, "external_reply", None)
    logger.info(
        "Telegram raw/normalized input:\n"
        "message.text=%r\nmessage.caption=%r\n"
        "reply_to_message.text=%r\nreply_to_message.caption=%r\n"
        "reply_to_message.entities=%r\nreply_to_message.caption_entities=%r\n"
        "reply_to_message.forward_origin=%r\n"
        "reply_to_message.photo=%s\nreply_to_message.document=%s\n"
        "external_reply=%r\nexternal_reply.photo=%s\n"
        "external_reply.document=%s\n"
        "message.quote=%r\nquote_text=%r\n"
        "reply_object_present=%s\nmessage.api_kwargs=%r\n"
        "reply_text_length=%s\nmessage.forward_origin=%r\nsource_type=%s\n"
        "photo=%s document=%s voice=%s\nnormalized_input=%r",
        normalized.main_text,
        normalized.caption,
        normalized.reply_text,
        normalized.reply_caption,
        getattr(replied, "entities", None),
        getattr(replied, "caption_entities", None),
        getattr(replied, "forward_origin", None),
        bool(getattr(replied, "photo", None)),
        bool(getattr(replied, "document", None)),
        external_reply,
        bool(getattr(external_reply, "photo", None)),
        bool(getattr(external_reply, "document", None)),
        getattr(update.message, "quote", None),
        normalized.quote_text,
        replied is not None,
        getattr(update.message, "api_kwargs", None),
        len(normalized.reply_text),
        getattr(update.message, "forward_origin", None),
        normalized.source_type,
        normalized.has_photo,
        normalized.has_document,
        normalized.has_voice,
        normalized.combined_text,
    )
    user_text = normalized.main_text or normalized.caption
    if not user_text:
        return
    if normalized.attachment_message and normalized.attachment_message is not update.message:
        await process_message_attachment(
            update,
            context,
            normalized.attachment_message,
            normalized.combined_text,
        )
        return

    inaccessible_reply = replied is not None and not any((
        normalized.reply_text,
        normalized.reply_caption,
        normalized.quote_text,
        normalized.attachment_message is not None,
        external_reply,
    ))
    if inaccessible_reply:
        await update.message.reply_text(
            "Я вижу, что ты ответила на сообщение, но Telegram Bot API передал "
            "мне только ссылку на него — без текста и изображения. Нажми на "
            "исходном сообщении «Переслать» → MargoPlanner (не «Ответить») "
            "или отправь его скриншотом/файлом — тогда я сразу прочитаю таблицу."
        )
        return

    vague_reference = user_text.casefold().strip(" ?!.,") in {
        "видишь", "видно", "добавь это", "обработай это", "а так",
    }
    saved_context = context.user_data.get("last_structured_input")
    if vague_reference and saved_context and not get_conversation(context):
        await process_universal_payload(
            update,
            context,
            InputPayload("forwarded_message", saved_context),
            user_text,
        )
        return
    if (
        vague_reference
        and not saved_context
        and not get_conversation(context)
        and not any((
            normalized.reply_text,
            normalized.reply_caption,
            normalized.quote_text,
            normalized.attachment_message,
            external_reply,
        ))
    ):
        await update.message.reply_text(
            "Пока нет: Telegram передал мне только сообщение «Видишь?», без "
            "видимой у тебя цитаты. Чтобы я прочитала таблицу, открой исходное "
            "сообщение, нажми «Переслать» и выбери MargoPlanner — не используй "
            "«Ответить». Ещё можно отправить скриншот отдельным фото."
        )
        return

    structured = is_structured_telegram_text(normalized)
    source_text = normalized.reply_text or normalized.quote_text or normalized.main_text
    if structured:
        context.user_data["last_structured_input"] = normalized.combined_text
    markdown_schedule = is_markdown_table(source_text)
    if structured and (markdown_schedule or looks_like_schedule(source_text)):
        message_key = f"forwarded:{update.effective_chat.id}:{update.message.message_id}"
        if not await asyncio.to_thread(
            input_dedup_store.claim,
            message_key,
            normalized.combined_text.encode("utf-8"),
        ):
            await update.message.reply_text(
                "Это расписание я уже недавно обработала. Используй готовый план выше."
            )
            return
        await update.message.reply_text("🔎 Читаю таблицу и ищу смены Марго...")
        plan = await asyncio.to_thread(parse_markdown_shifts, source_text)
        if plan is None:
            await process_universal_payload(
                update,
                context,
                InputPayload("forwarded_message", normalized.combined_text),
                user_text,
            )
            return
        if len(source_text) >= 4090:
            plan.setdefault("notes", []).append(
                "Telegram передал текст предельной длины. Возможно, конец "
                "таблицы обрезан; проверь найденные строки перед подтверждением."
            )
        await present_universal_plan(
            update,
            context,
            plan,
            source_text,
            user_text if normalized.reply_text else "Расписание Валеры",
        )
        return
    if structured:
        await process_universal_payload(
            update,
            context,
            InputPayload("forwarded_message", normalized.combined_text),
            user_text,
        )
        return
    await process_user_text(update, context, normalized.combined_text or user_text)


async def process_universal_payload(update, context, payload, user_request=""):
    await update.effective_message.reply_text("🔎 Читаю и составляю план...")
    extracted = await asyncio.to_thread(extract_content, payload)
    logger.info(
        "Planner input: source_type=%s request=%r extracted=%r",
        payload.source_type,
        str(user_request),
        str(extracted),
    )
    memories = await asyncio.to_thread(memory_store.as_prompt_context)
    plan = await asyncio.to_thread(
        build_plan,
        extracted,
        user_request or payload.caption,
        memories,
    )
    await present_universal_plan(update, context, plan, extracted, user_request)


async def present_universal_plan(update, context, plan, extracted, user_request=""):
    if plan["clarification_question"]:
        conversation = new_conversation(user_request or "Импорт")
        conversation["state"] = ConversationState.WAITING_FOR_CLARIFICATION
        conversation["draft"] = {
            "operation": "universal_plan_edit",
            "plan": plan,
            "extracted": extracted if isinstance(extracted, str) else "Изображение",
        }
        save_conversation(context, conversation)
        await update.effective_message.reply_text(plan["clarification_question"])
        return
    analysis = await asyncio.to_thread(analyze_batch, plan)
    counts = batch_counts(analysis)
    conversation = new_conversation(user_request or "Импорт")
    conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
    conversation["draft"] = {
        "operation": "universal_batch_review" if counts["remaining"] else "universal_plan",
        "plan": plan,
        "batch_analysis": analysis,
        "extracted": extracted if isinstance(extracted, str) else "Изображение",
    }
    save_conversation(context, conversation)
    notes = [str(note).strip() for note in plan.get("notes", []) if str(note).strip()]
    notes_text = "\n\n⚠️ " + "\n⚠️ ".join(notes) if notes else ""
    await update.effective_message.reply_text(
        "Я обработала весь список.\n\n"
        + format_batch_report(analysis)
        + "\n\nПолный план:\n\n"
        + format_plan(plan)
        + notes_text,
        reply_markup=None if counts["remaining"] else plan_keyboard(),
    )
    if counts["remaining"]:
        await show_next_batch_conflict(
            update.effective_message,
            context,
            conversation,
        )


async def show_next_batch_conflict(message, context, conversation):
    draft = conversation["draft"]
    analysis = draft["batch_analysis"]
    unresolved = [
        item for item in analysis
        if item["classification"] in {"conflict", "exact_duplicate"}
        and item["decision"] is None
    ]
    if not unresolved:
        draft["operation"] = "universal_plan"
        conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
        save_conversation(context, conversation)
        await message.reply_text(
            "Все конфликты разобраны. Batch готов к выполнению.",
            reply_markup=plan_keyboard(),
        )
        return
    current = unresolved[0]
    draft["current_conflict_index"] = current["action_index"]
    save_conversation(context, conversation)
    await message.reply_text(
        (
            format_duplicate(draft["plan"], current)
            if current["classification"] == "exact_duplicate"
            else format_conflict(draft["plan"], current)
        )
        + f"\n\nОсталось принять решений: {len(unresolved)}",
        reply_markup=(
            duplicate_keyboard()
            if current["classification"] == "exact_duplicate"
            else conflict_keyboard()
        ),
    )


async def execute_saved_batch(message, context, user_id, conversation):
    draft = conversation["draft"]
    if draft.get("batch_analysis"):
        summary = await asyncio.to_thread(
            execute_batch,
            draft["plan"],
            draft["batch_analysis"],
            user_id,
            reminder_store,
        )
    else:
        results = await asyncio.to_thread(
            execute_plan,
            draft["plan"],
            user_id,
            reminder_store,
            draft.get("duplicate_indexes", []),
        )
        summary = summarize_plan_execution(draft["plan"], results)
    context.user_data["last_batch_report"] = summary
    clear_conversation(context)
    await message.reply_text(
        "Готово. Итог batch:\n\n" + format_execution_report(summary)
    )


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize_update(update):
        return
    await process_message_attachment(update, context, update.effective_message)


async def process_message_attachment(update, context, message, user_request=""):
    source_type = detect_message_input(message)
    attachment = get_message_attachment(message)
    logger.info(
        "Telegram attachment received: photo=%s document=%s animation=%s "
        "video=%s sticker=%s detected=%s",
        bool(getattr(message, "photo", None)),
        bool(getattr(message, "document", None)),
        bool(getattr(message, "animation", None)),
        bool(getattr(message, "video", None)),
        bool(getattr(message, "sticker", None)),
        source_type,
    )
    if attachment is None:
        await message.reply_text(
            "Я получила вложение, но пока не могу прочитать этот формат. "
            "Пришли его как фото или файл."
        )
        return
    if source_type == "document":
        await message.reply_text(
            "Пока я читаю изображения, PDF, CSV и XLSX. Этот формат не поддерживается."
        )
        return
    media, filename, mime_type = attachment
    telegram_file = await context.bot.get_file(media.file_id)
    file_id = media.file_unique_id or media.file_id
    content = bytes(await telegram_file.download_as_bytearray())
    if not await asyncio.to_thread(input_dedup_store.claim, file_id, content):
        await message.reply_text(
            "Этот файл я уже недавно обработала. Используй готовый план выше "
            "или нажми «✏️ Исправить»."
        )
        return
    payload = InputPayload(
        source_type=source_type,
        content=content,
        filename=filename,
        mime_type=mime_type,
        caption=message.caption or "",
    )
    await process_universal_payload(
        update,
        context,
        payload,
        user_request or message.caption or "",
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize_update(update):
        return

    voice = update.message.voice
    if voice.duration > MAX_VOICE_DURATION_SECONDS:
        await update.message.reply_text(
            "Голосовое слишком длинное. Пока я принимаю записи до 10 минут."
        )
        return
    if voice.file_size and voice.file_size > MAX_VOICE_SIZE_BYTES:
        await update.message.reply_text(
            "Голосовое слишком большое. Максимальный размер — 15 МБ."
        )
        return

    telegram_file = await context.bot.get_file(voice.file_id)
    audio = await telegram_file.download_as_bytearray()
    await update.message.reply_text("🎙 Слушаю и разбираю...")
    try:
        transcript = await asyncio.to_thread(
            transcribe_voice,
            bytes(audio),
            voice.mime_type or "audio/ogg",
        )
    except VoiceQuotaError as error:
        if error.retry_after_seconds:
            retry_text = f"Попробуйте ещё раз через {error.retry_after_seconds} сек."
        else:
            retry_text = "Попробуйте ещё раз немного позже."
        await update.message.reply_text(
            "Я упёрлась в лимит распознавания речи. " + retry_text
        )
        return
    await update.message.reply_text(f"Я услышала: «{transcript}»")
    await process_user_text(update, context, transcript)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if ALLOWED_USER_ID is not None and update.effective_user.id != ALLOWED_USER_ID:
        return
    data = query.data or ""
    proxy = SimpleNamespace(
        message=query.message,
        effective_message=query.message,
        effective_user=update.effective_user,
    )

    if data.startswith("followup:"):
        parts = data.split(":")
        if (
            len(parts) != 3
            or parts[1] not in {"done", "forgot", "snooze", "close"}
            or not parts[2].isdigit()
        ):
            await query.message.reply_text("Этот ответ уже неактуален.")
            return
        reminder_id = int(parts[2])
        if parts[1] == "snooze":
            remind_at = datetime.now(LOCAL_TIMEZONE) + timedelta(hours=1)
            repeated = await asyncio.to_thread(
                reminder_store.repeat_forgotten,
                update.effective_user.id,
                reminder_id,
                remind_at.isoformat(),
            )
            if repeated:
                await query.message.reply_text(
                    "Хорошо, напомню ещё через час 🔔\n\n"
                    f"{repeated['text']}"
                )
            else:
                await query.message.reply_text("Этот выбор уже неактуален.")
            return
        if parts[1] == "close":
            dismissed = await asyncio.to_thread(
                reminder_store.dismiss_forgotten,
                update.effective_user.id,
                reminder_id,
            )
            await query.message.reply_text(
                "Хорошо, больше не напоминаю."
                if dismissed else "Этот выбор уже неактуален."
            )
            return
        saved = await asyncio.to_thread(
            reminder_store.answer_followup,
            update.effective_user.id,
            reminder_id,
            parts[1],
        )
        if not saved:
            await query.message.reply_text("На это напоминание уже ответили.")
        elif parts[1] == "done":
            await query.message.reply_text("Отлично, отметила как выполненное ✅")
        else:
            await query.message.reply_text(
                "Поняла 🙈 Тогда напомнить тебе ещё через час?",
                reply_markup=reminder_repeat_keyboard(reminder_id),
            )
        return

    if data == "confirm:yes":
        await process_user_text(proxy, context, "да")
        return
    if data == "confirm:no":
        await process_user_text(proxy, context, "нет")
        return
    if data == "cancel":
        clear_conversation(context)
        await query.message.reply_text("Хорошо, отменила.")
        return

    conversation = get_conversation(context)
    if data.startswith("multiselect:"):
        if not conversation or conversation.get("draft", {}).get("operation") != "update_event":
            await query.message.reply_text("Этот список уже неактуален.")
            return
        draft = conversation["draft"]
        selected = set(draft.get("selected_candidate_indexes", []))
        if data.startswith("multiselect:event:"):
            raw_index = data.rsplit(":", 1)[1]
            if not raw_index.isdigit() or int(raw_index) >= len(draft["candidates"]):
                await query.message.reply_text("Этот вариант уже неактуален.")
                return
            index = int(raw_index)
            if index in selected:
                selected.remove(index)
            else:
                selected.add(index)
            draft["selected_candidate_indexes"] = sorted(selected)
            save_conversation(context, conversation)
            await query.edit_message_reply_markup(
                reply_markup=multi_event_selection_keyboard(
                    draft["candidates"], selected
                )
            )
            return
        if data == "multiselect:done":
            if not selected:
                await query.message.reply_text(
                    "Отметь хотя бы одно событие, затем нажми «Готово»."
                )
                return
            proposed = (draft.get("events") or [None])[0]
            if not proposed:
                await query.message.reply_text("Не вижу новой даты переноса.")
                return
            targets, events = build_group_update_events(
                draft["candidates"], sorted(selected), proposed
            )
            draft["operation"] = "update_events"
            draft["targets"] = targets
            draft["events"] = events
            draft["conflicts"] = await asyncio.to_thread(
                find_conflicts, events, None, {item["id"] for item in targets}
            )
            conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
            save_conversation(context, conversation)
            warning = format_conflicts(draft["conflicts"])
            await query.message.reply_text(
                "Перенести связанные события:\n\n"
                + format_events(targets)
                + "\n\nНа:\n\n"
                + format_events(events)
                + ("\n\n" + warning if warning else "")
                + "\n\nПодтвердить?",
                reply_markup=confirmation_keyboard(),
            )
            return
    if data.startswith("batchall:"):
        if not conversation or conversation.get("draft", {}).get("operation") not in {
            "universal_batch_review",
            "universal_plan",
        }:
            await query.message.reply_text("Этот batch уже неактуален.")
            return
        draft = conversation["draft"]
        choice = data.split(":", 1)[1]
        if choice == "review":
            draft["operation"] = "universal_batch_review"
            save_conversation(context, conversation)
            await show_next_batch_conflict(query.message, context, conversation)
            return
        for entry in draft.get("batch_analysis", []):
            if choice == "all":
                entry["decision"] = "create"
            elif entry["classification"] == "exact_duplicate":
                entry["decision"] = "skip"
            elif choice == "safe" and entry["classification"] == "conflict":
                entry["decision"] = "skip"
        if choice == "dedupe":
            unresolved = [
                entry for entry in draft.get("batch_analysis", [])
                if entry["classification"] == "conflict" and entry["decision"] is None
            ]
            if unresolved:
                draft["operation"] = "universal_batch_review"
                save_conversation(context, conversation)
                await query.message.reply_text(
                    "Точные дубли исключены. Теперь разберём пересечения."
                )
                await show_next_batch_conflict(query.message, context, conversation)
                return
        await execute_saved_batch(
            query.message,
            context,
            update.effective_user.id,
            conversation,
        )
        return
    if data.startswith("batch:"):
        if not conversation or conversation.get("draft", {}).get("operation") != "universal_batch_review":
            await query.message.reply_text("Этот batch уже неактуален.")
            return
        draft = conversation["draft"]
        action_index = draft.get("current_conflict_index")
        entry = next(
            item for item in draft["batch_analysis"]
            if item["action_index"] == action_index
        )
        choice = data.split(":", 1)[1]
        if choice == "edit":
            draft["operation"] = "universal_batch_edit"
            conversation["state"] = ConversationState.WAITING_FOR_CLARIFICATION
            save_conversation(context, conversation)
            await query.message.reply_text(
                "Как изменить новое событие? Напиши новую дату, время или другие детали."
            )
            return
        entry["decision"] = {
            "keep": "skip",
            "replace": "replace",
            "both": "create",
            "cancel": "cancel",
        }[choice]
        await show_next_batch_conflict(query.message, context, conversation)
        return
    if data == "plan:execute":
        if not conversation or conversation.get("draft", {}).get("operation") != "universal_plan":
            await query.message.reply_text("Этот план уже неактуален.")
            return
        await execute_saved_batch(
            query.message,
            context,
            update.effective_user.id,
            conversation,
        )
        return
    if data == "plan:edit":
        if not conversation or conversation.get("draft", {}).get("operation") not in {
            "universal_plan",
            "universal_batch_review",
        }:
            await query.message.reply_text("Этот план уже неактуален.")
            return
        conversation["state"] = ConversationState.WAITING_FOR_CLARIFICATION
        conversation["draft"]["operation"] = "universal_plan_edit"
        save_conversation(context, conversation)
        await query.message.reply_text("Что исправить в плане? Напиши своими словами.")
        return
    if data == "reminders:add":
        clear_conversation(context)
        await query.message.reply_text(
            "Что тебе напомнить и когда? Напиши как обычно, своими словами."
        )
        return
    if not conversation:
        await query.message.reply_text(
            "Этот список уже неактуален. Попроси меня показать его ещё раз."
        )
        return

    draft = conversation.get("draft", {})
    reminder_candidates = draft.get("reminder_candidates", [])
    if data == "reminders:delete":
        if not reminder_candidates:
            await query.message.reply_text("Активных напоминаний уже нет.")
            return
        conversation["state"] = ConversationState.WAITING_FOR_CLARIFICATION
        draft["operation"] = "delete_reminder"
        save_conversation(context, conversation)
        await query.message.reply_text(
            "Какое напоминание удалить?",
            reply_markup=selection_keyboard(reminder_candidates, "reminder"),
        )
        return
    if data == "reminders:clear":
        if not reminder_candidates:
            await query.message.reply_text("Активных напоминаний уже нет.")
            return
        conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
        conversation["draft"] = {
            "operation": "delete_reminders",
            "events": [],
            "reminder_targets": reminder_candidates,
        }
        save_conversation(context, conversation)
        await query.message.reply_text(
            "Удалить все показанные напоминания?\n\n"
            + format_reminder_list(reminder_candidates),
            reply_markup=confirmation_keyboard(),
        )
        return
    if data.startswith("select:"):
        parts = data.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            return
        index = int(parts[2])
        candidates = (
            reminder_candidates
            if parts[1] == "reminder"
            else draft.get("candidates", [])
        )
        if index >= len(candidates):
            await query.message.reply_text("Этот вариант уже неактуален.")
            return
        await process_user_text(proxy, context, str(index + 1))


async def process_user_text(update, context, user_text):
    normalized_text = user_text.lower()

    conversation = get_conversation(context)

    if not conversation:
        last_batch_report = context.user_data.get("last_batch_report")
        retry_action = (
            find_retry_action(user_text, last_batch_report)
            if last_batch_report else None
        )
        if retry_action:
            plan = {
                "actions": [retry_action],
                "clarification_question": "",
                "notes": [],
            }
            analysis = await asyncio.to_thread(analyze_batch, plan)
            if analysis[0]["classification"] == "exact_duplicate":
                analysis[0]["decision"] = "create"
            conversation = new_conversation(user_text)
            conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
            conversation["draft"] = {
                "operation": (
                    "universal_batch_review"
                    if analysis[0]["decision"] is None else "universal_plan"
                ),
                "plan": plan,
                "batch_analysis": analysis,
                "extracted": "Повтор элемента из последнего batch",
            }
            save_conversation(context, conversation)
            await update.message.reply_text(
                "Нашла этот элемент в последнем batch:\n\n"
                + format_plan(plan),
                reply_markup=(
                    None if analysis[0]["decision"] is None else plan_keyboard()
                ),
            )
            if analysis[0]["decision"] is None:
                await show_next_batch_conflict(update.message, context, conversation)
            return
        followup = (
            batch_followup_response(user_text, last_batch_report)
            if last_batch_report else None
        )
        if followup:
            await update.message.reply_text(followup)
            return

    if normalized_text in UNDO_REQUESTS:
        action = await asyncio.to_thread(action_history_store.get_last_active)
        if action is None:
            await update.message.reply_text(
                "У меня нет выполненного календарного действия, которое можно отменить."
            )
            return
        conversation = new_conversation(user_text)
        conversation["state"] = ConversationState.WAITING_FOR_UNDO_CONFIRMATION
        conversation["draft"] = {"undo_action": action}
        save_conversation(context, conversation)
        await update.message.reply_text(
            format_undo_action(action)
            + "\n\nОтменить это действие?",
            reply_markup=confirmation_keyboard(),
        )
        return

    if conversation:
        state = conversation["state"]

        if state == ConversationState.WAITING_FOR_UNDO_CONFIRMATION:
            if normalized_text in YES_ANSWERS:
                action = conversation["draft"]["undo_action"]
                result_text = await asyncio.to_thread(undo_calendar_action, action)
                await asyncio.to_thread(
                    action_history_store.mark_undone,
                    action["id"],
                )
                clear_conversation(context)
                await update.message.reply_text(result_text)
                return
            if normalized_text in NO_ANSWERS:
                clear_conversation(context)
                await update.message.reply_text("Хорошо, ничего не отменяю.")
                return

        if state == ConversationState.WAITING_FOR_CLARIFICATION:
            draft = conversation.get("draft", {})
            if draft.get("operation") == "universal_batch_edit":
                action_index = draft["current_conflict_index"]
                current_action = draft["plan"]["actions"][action_index]
                memories = await asyncio.to_thread(memory_store.as_prompt_context)
                revised = await asyncio.to_thread(
                    build_plan,
                    str(current_action),
                    "Исправление пользователя: " + user_text,
                    memories,
                )
                if revised["clarification_question"] or len(revised["actions"]) != 1:
                    await update.message.reply_text(
                        revised["clarification_question"]
                        or "Опиши одно новое время для этого события."
                    )
                    return
                draft["plan"]["actions"][action_index] = revised["actions"][0]
                refreshed = await asyncio.to_thread(
                    analyze_batch,
                    {"actions": [revised["actions"][0]]},
                )
                refreshed[0]["action_index"] = action_index
                draft["batch_analysis"] = [
                    refreshed[0] if item["action_index"] == action_index else item
                    for item in draft["batch_analysis"]
                ]
                draft["operation"] = "universal_batch_review"
                conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
                save_conversation(context, conversation)
                await update.message.reply_text("Новое событие обновлено.")
                await show_next_batch_conflict(update.message, context, conversation)
                return
            if draft.get("operation") == "universal_plan_edit":
                source = (
                    str(draft.get("extracted", ""))
                    + "\n\nТекущий план:\n"
                    + str(draft.get("plan", {}))
                )
                memories = await asyncio.to_thread(memory_store.as_prompt_context)
                plan = await asyncio.to_thread(
                    build_plan,
                    source,
                    "Исправление пользователя: " + user_text,
                    memories,
                )
                if plan["clarification_question"]:
                    draft["plan"] = plan
                    save_conversation(context, conversation)
                    await update.message.reply_text(plan["clarification_question"])
                    return
                analysis = await asyncio.to_thread(analyze_batch, plan)
                counts = batch_counts(analysis)
                conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
                draft["operation"] = (
                    "universal_batch_review" if counts["remaining"] else "universal_plan"
                )
                draft["plan"] = plan
                draft["batch_analysis"] = analysis
                save_conversation(context, conversation)
                await update.message.reply_text(
                    "Обновила весь batch:\n\n"
                    + format_batch_report(analysis)
                    + "\n\n"
                    + format_plan(plan),
                    reply_markup=None if counts["remaining"] else plan_keyboard(),
                )
                if counts["remaining"]:
                    await show_next_batch_conflict(
                        update.message, context, conversation
                    )
                return
            reminder_candidates = draft.get("reminder_candidates", [])
            reminder_indexes = parse_candidate_selection(
                user_text,
                len(reminder_candidates),
            )
            if (
                reminder_indexes is not None
                and draft.get("operation") == "delete_reminder"
            ):
                targets = [reminder_candidates[index] for index in reminder_indexes]
                draft["operation"] = (
                    "delete_reminders" if len(targets) > 1 else "delete_reminder"
                )
                draft["reminder_targets"] = targets
                conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
                save_conversation(context, conversation)
                await update.message.reply_text(
                    "Удалить напоминания:\n\n"
                    + format_reminder_list(targets)
                    + "\n\nПодтвердить?",
                    reply_markup=confirmation_keyboard(),
                )
                return
            candidates = draft.get("candidates", [])
            selected_indexes = parse_candidate_selection(
                user_text,
                len(candidates),
            )
            if selected_indexes is not None and draft.get("operation") in {
                "delete_event",
                "delete_events",
            }:
                targets = [candidates[index] for index in selected_indexes]
                draft["operation"] = (
                    "delete_events" if len(targets) > 1 else "delete_event"
                )
                draft["targets"] = targets
                draft["target"] = targets[0]
                draft["target_event_id"] = targets[0]["id"]
                conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
                save_conversation(context, conversation)
                await update.message.reply_text(
                    "Удалить события:\n\n"
                    + format_events(targets)
                    + "\n\nПодтвердить?",
                    reply_markup=confirmation_keyboard(),
                )
                return

        if state == ConversationState.WAITING_FOR_CONFIRMATION:
            draft = conversation["draft"]
            operation = draft.get("operation", "create_events")
            events = draft.get("events", [])

            if normalized_text in YES_ANSWERS:
                if operation == "forget_memories":
                    forgotten = await asyncio.to_thread(
                        memory_store.apply_updates,
                        draft["memory_updates"],
                    )
                    clear_conversation(context)
                    await update.message.reply_text(
                        "Готово, забыла:\n\n" + format_memory_updates(forgotten)
                    )
                    return
                if operation == "universal_plan":
                    if draft.get("batch_analysis"):
                        summary = await asyncio.to_thread(
                            execute_batch,
                            draft["plan"],
                            draft["batch_analysis"],
                            update.effective_user.id,
                            reminder_store,
                        )
                    else:
                        results = await asyncio.to_thread(
                            execute_plan,
                            draft["plan"],
                            update.effective_user.id,
                            reminder_store,
                            draft.get("duplicate_indexes", []),
                        )
                        summary = summarize_plan_execution(draft["plan"], results)
                    context.user_data["last_batch_report"] = summary
                    clear_conversation(context)
                    await update.message.reply_text(
                        "Готово. Итог batch:\n\n"
                        + format_execution_report(summary)
                    )
                    return
                if operation in {"delete_reminder", "delete_reminders"}:
                    targets = draft["reminder_targets"]
                    deleted = await asyncio.to_thread(
                        reminder_store.delete_pending,
                        update.effective_user.id,
                        [item["id"] for item in targets],
                    )
                    clear_conversation(context)
                    await update.message.reply_text(
                        f"Готово. Удалила напоминаний: {deleted}."
                    )
                    return
                if operation == "create_reminder":
                    reminder = draft["reminder"]
                    await asyncio.to_thread(
                        reminder_store.create,
                        update.effective_user.id,
                        reminder["text"],
                        reminder["remind_at"],
                    )
                    clear_conversation(context)
                    await update.message.reply_text(
                        "Готово, напомню тебе в Telegram. 🔔\n\n"
                        + format_reminder(reminder)
                    )
                    return
                if operation == "create_events":
                    excluded_ids = batch_event_ids(draft["batch_id"], len(events))
                elif operation == "update_events":
                    excluded_ids = {event["id"] for event in draft["targets"]}
                    current_targets = []
                    changed_targets = False
                    for saved_target in draft["targets"]:
                        current_target = await asyncio.to_thread(
                            get_event, saved_target["id"]
                        )
                        current_targets.append(current_target)
                        changed_targets = changed_targets or _event_changed(
                            saved_target, current_target
                        )
                    if changed_targets:
                        draft["targets"] = current_targets
                        save_conversation(context, conversation)
                        await update.message.reply_text(
                            "Одно из связанных событий изменилось. "
                            "Проверь перенос и подтверди ещё раз.",
                            reply_markup=confirmation_keyboard(),
                        )
                        return
                elif operation == "delete_events":
                    excluded_ids = {event["id"] for event in draft["targets"]}
                    current_targets = []
                    changed_targets = False
                    for saved_target in draft["targets"]:
                        current_target = await asyncio.to_thread(
                            get_event,
                            saved_target["id"],
                        )
                        current_targets.append(current_target)
                        changed_targets = changed_targets or _event_changed(
                            saved_target,
                            current_target,
                        )
                    if changed_targets:
                        draft["targets"] = current_targets
                        save_conversation(context, conversation)
                        await update.message.reply_text(
                            "Одно из событий изменилось после моего предложения. "
                            "Я обновила список — подтвердите удаление ещё раз.",
                            reply_markup=confirmation_keyboard(),
                        )
                        return
                else:
                    excluded_ids = {draft["target_event_id"]}
                    current_target = await asyncio.to_thread(
                        get_event,
                        draft["target_event_id"],
                    )
                    if _event_changed(draft["target"], current_target):
                        draft["target"] = current_target
                        save_conversation(context, conversation)
                        await update.message.reply_text(
                            "Событие изменилось в календаре после моего "
                            "предложения. Я обновила данные — подтвердите "
                            "действие ещё раз.",
                            reply_markup=confirmation_keyboard(),
                        )
                        return

                if operation in {"create_events", "update_event", "update_events"}:
                    current_conflicts = await asyncio.to_thread(
                        find_conflicts,
                        events,
                        None,
                        excluded_ids,
                    )
                    shown_conflicts = draft.get("conflicts", [])
                else:
                    current_conflicts = []
                    shown_conflicts = []

                if _conflict_signature(current_conflicts) != _conflict_signature(
                    shown_conflicts
                ):
                    draft["conflicts"] = current_conflicts
                    save_conversation(context, conversation)
                    warning = format_conflicts(current_conflicts)
                    await update.message.reply_text(
                        (warning + "\n\n" if warning else "")
                        + "Календарь изменился после моего предложения. "
                        "Выполнить действие с учётом новой ситуации?"
                    )
                    return

                if operation == "create_events":
                    created = await asyncio.to_thread(
                        create_events,
                        events,
                        draft["batch_id"],
                    )
                    result_text = (
                        "Готово. Всё добавлено в календарь. 🗓\n\n"
                        + "\n".join(
                            event.get("htmlLink", "") for event in created
                        )
                    )
                    await asyncio.to_thread(
                        action_history_store.record,
                        "create_events",
                        {
                            "events": [
                                {**source, "id": result.get("id", "")}
                                for source, result in zip(events, created)
                            ]
                        },
                    )
                elif operation == "update_event":
                    changed = await asyncio.to_thread(
                        update_event,
                        draft["target_event_id"],
                        events[0],
                    )
                    result_text = (
                        "Готово. Событие изменено. 🗓\n"
                        + changed.get("htmlLink", "")
                    )
                    await asyncio.to_thread(
                        action_history_store.record,
                        "update_event",
                        {
                            "event_id": draft["target_event_id"],
                            "before": draft["target"],
                            "after": events[0],
                        },
                    )
                elif operation == "update_events":
                    changed = []
                    for target, event in zip(draft["targets"], events):
                        result = await asyncio.to_thread(
                            update_event, target["id"], event
                        )
                        changed.append(result)
                    result_text = (
                        f"Готово. Перенесла связанных событий: {len(changed)}. 🗓"
                    )
                    await asyncio.to_thread(
                        action_history_store.record,
                        "update_events",
                        {"updates": [
                            {
                                "event_id": target["id"],
                                "before": target,
                                "after": event,
                            }
                            for target, event in zip(draft["targets"], events)
                        ]},
                    )
                elif operation == "delete_event":
                    await asyncio.to_thread(
                        delete_event,
                        draft["target_event_id"],
                    )
                    result_text = "Готово. Событие удалено из календаря."
                    await asyncio.to_thread(
                        action_history_store.record,
                        "delete_events",
                        {"events": [draft["target"]]},
                    )
                else:
                    await asyncio.to_thread(
                        delete_calendar_events,
                        [event["id"] for event in draft["targets"]],
                    )
                    result_text = (
                        f"Готово. Удалила событий: {len(draft['targets'])}."
                    )
                    await asyncio.to_thread(
                        action_history_store.record,
                        "delete_events",
                        {"events": draft["targets"]},
                    )

                clear_conversation(context)
                await update.message.reply_text(result_text)
                return

            if normalized_text in NO_ANSWERS:
                clear_conversation(context)
                if operation == "forget_memories":
                    await update.message.reply_text(
                        "Хорошо, ничего не забываю. Память осталась как была."
                    )
                elif operation == "create_reminder":
                    await update.message.reply_text("Хорошо, напоминание не ставлю.")
                else:
                    await update.message.reply_text(
                        "Отменила. Календарь остался невредим."
                    )
                return

        conversation = add_user_message(conversation, user_text)
    else:
        conversation = new_conversation(user_text)

    # Persist the latest user message before an external API call. If Gemini is
    # temporarily unavailable, the next message can continue the same thread.
    save_conversation(context, conversation)
    inferred_memories = infer_stable_memories(user_text)
    if inferred_memories:
        await asyncio.to_thread(memory_store.apply_updates, inferred_memories)
    memories = await asyncio.to_thread(memory_store.as_prompt_context)
    explicit_reminder = parse_explicit_reminder_request(user_text)
    shift_events = parse_shift_schedule_request(user_text)
    if explicit_reminder:
        intent = {
            "action": "create_reminder",
            "clarification_question": "",
            "reason": "Явно указаны время и текст напоминания",
            "events": [],
            "memory_updates": [],
            "reminder": explicit_reminder,
        }
    elif shift_events:
        intent = {
            "action": "create_events",
            "clarification_question": "",
            "reason": "Распознано недельное расписание смен",
            "events": shift_events,
            "memory_updates": inferred_memories,
        }
    elif (
        conversation.get("state") == ConversationState.IDLE
        and is_multi_action_request(user_text)
    ):
        logger.info("Multi-action planner input: %r", user_text[:6000])
        plan = await asyncio.to_thread(
            build_plan,
            user_text,
            user_text,
            memories,
        )
        await present_universal_plan(
            update,
            context,
            plan,
            user_text,
            user_text,
        )
        return
    else:
        calendar_list_intent = parse_calendar_list_request(user_text)
        if calendar_list_intent:
            intent = calendar_list_intent
        else:
            logger.info("Intent input: %r", user_text[:6000])
            intent = await asyncio.to_thread(
                detect_intent,
                user_text,
                conversation,
                memories,
            )
    action = intent.get("action")

    if action != "forget_memory":
        applied_memory_updates = await asyncio.to_thread(
            memory_store.apply_updates,
            intent.get("memory_updates", []),
        )
    else:
        applied_memory_updates = []

    # Choosing from real calendar data is more useful than an abstract
    # clarification for requests such as "отмени событие".  Keep Gemini for
    # understanding detailed requests, but make this common UX deterministic.
    if action == "clarify":
        delete_intent = _generic_calendar_delete_intent(user_text)
        if delete_intent:
            intent = delete_intent
            action = intent["action"]

    if action == "clarify":
        conversation = apply_intent(conversation, intent)
        save_conversation(context, conversation)

        await update.message.reply_text(
            intent.get(
                "clarification_question",
                "Уточните, пожалуйста, недостающие детали.",
            )
        )
        return

    if action == "list_memories":
        saved_memories = await asyncio.to_thread(memory_store.get_all)
        clear_conversation(context)
        if saved_memories:
            await update.message.reply_text(
                "Вот что я запомнила из наших разговоров:\n\n"
                + format_memories(saved_memories)
                + "\n\nМожешь попросить исправить или забыть любой факт."
            )
        else:
            await update.message.reply_text(
                "Пока в моей пополняемой памяти нет отдельных фактов. "
                "Но базовый профиль о тебе у меня есть. Можешь написать: "
                "«Запомни, что…»"
            )
        return

    if action == "show_calendar":
        search = intent["search"]
        events = await asyncio.to_thread(search_events, search)
        clear_conversation(context)
        day = datetime.fromisoformat(search["time_min"]).astimezone(
            LOCAL_TIMEZONE
        )
        if events:
            await update.message.reply_text(
                f"Вот что стоит в календаре на {day.strftime('%d.%m.%Y')}:\n\n"
                + format_events(events)
            )
        else:
            await update.message.reply_text(
                f"На {day.strftime('%d.%m.%Y')} в календаре ничего нет. ✨"
            )
        return

    if action == "remember_memory":
        clear_conversation(context)
        await update.message.reply_text(
            "Запомнила 🧠\n\n" + format_memory_updates(applied_memory_updates)
        )
        return

    if action == "forget_memory":
        updates = intent.get("memory_updates", [])
        conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
        conversation["draft"] = {
            "operation": "forget_memories",
            "events": [],
            "memory_updates": updates,
        }
        save_conversation(context, conversation)
        await update.message.reply_text(
            "Забыть из долговременной памяти:\n\n"
            + format_memory_updates(updates)
            + "\n\nПодтвердить?",
            reply_markup=confirmation_keyboard(),
        )
        return

    if action == "create_events":
        events = intent.get("events", [])

        if not events:
            await update.message.reply_text(
                "Я поняла задачу, но не смогла подготовить события."
            )
            return

        conversation = apply_intent(conversation, intent)
        conflicts = await asyncio.to_thread(
            find_conflicts,
            events,
            None,
            batch_event_ids(conversation["draft"]["batch_id"], len(events)),
        )
        conversation["draft"]["conflicts"] = conflicts
        save_conversation(context, conversation)

        warning = format_conflicts(conflicts)
        await update.message.reply_text(
            "Я поняла так:\n\n"
            + format_events(events)
            + ("\n\n" + warning if warning else "")
            + "\n\nСоздать?",
            reply_markup=confirmation_keyboard(),
        )
        return

    if action == "create_reminder":
        reminder = intent["reminder"]
        conversation = apply_intent(conversation, intent)
        save_conversation(context, conversation)
        await update.message.reply_text(
            "Поставить напоминание в Telegram:\n\n"
            + format_reminder(reminder)
            + "\n\nПодтвердить?",
            reply_markup=confirmation_keyboard(),
        )
        return

    if action == "list_reminders":
        search = intent["search"]
        reminders = await asyncio.to_thread(
            reminder_store.list_pending,
            update.effective_user.id,
            search["time_min"],
            search["time_max"],
        )
        if reminders:
            conversation["state"] = ConversationState.IDLE
            conversation["draft"] = {
                "operation": "list_reminders",
                "events": [],
                "reminder_candidates": reminders,
            }
            save_conversation(context, conversation)
            await update.message.reply_text(
                "Вот твои активные напоминания:\n\n"
                + format_reminder_list(reminders),
                reply_markup=reminder_actions_keyboard(),
            )
        else:
            clear_conversation(context)
            await update.message.reply_text(
                "На этот период активных напоминаний нет. ✨"
            )
        return

    if action in {"delete_reminder", "delete_reminders"}:
        previous = conversation.get("draft", {}).get("reminder_candidates", [])
        requested_ids = set(intent.get("target_reminder_ids", []))
        targets = [item for item in previous if item["id"] in requested_ids]
        if not targets:
            targets = await asyncio.to_thread(
                reminder_store.search_pending,
                update.effective_user.id,
                intent.get("search", {}),
            )
        if not targets:
            clear_conversation(context)
            await update.message.reply_text("Подходящих активных напоминаний нет.")
            return
        if action == "delete_reminder" and len(targets) > 1:
            conversation["state"] = ConversationState.WAITING_FOR_CLARIFICATION
            conversation["draft"] = {
                "operation": "delete_reminder",
                "events": [],
                "reminder_candidates": targets,
            }
            save_conversation(context, conversation)
            await update.message.reply_text(
                "Нашла несколько напоминаний:\n\n"
                + format_reminder_list(targets)
                + "\n\nКакое удалить?",
                reply_markup=selection_keyboard(targets, "reminder"),
            )
            return
        conversation["state"] = ConversationState.WAITING_FOR_CONFIRMATION
        conversation["draft"] = {
            "operation": "delete_reminders" if len(targets) > 1 else "delete_reminder",
            "events": [],
            "reminder_targets": targets,
        }
        save_conversation(context, conversation)
        await update.message.reply_text(
            "Удалить напоминания:\n\n"
            + format_reminder_list(targets)
            + "\n\nПодтвердить?",
            reply_markup=confirmation_keyboard(),
        )
        return

    if action in {"update_event", "delete_event", "delete_events"}:
        previous_candidates = conversation.get("draft", {}).get("candidates", [])
        selected_id = intent.get("target_event_id", "")
        target = next(
            (
                candidate
                for candidate in previous_candidates
                if candidate["id"] == selected_id
            ),
            None,
        )

        conversation = apply_intent(conversation, intent)
        if target is None:
            candidates = await asyncio.to_thread(
                search_calendar_candidates,
                intent.get("search", {}),
                user_text,
            )
            if not candidates:
                conversation["state"] = ConversationState.WAITING_FOR_CLARIFICATION
                conversation["draft"]["candidates"] = candidates
                save_conversation(context, conversation)
                await update.message.reply_text(
                    "Я не нашла подходящее событие. Подскажите его "
                    "название или дату точнее."
                )
                return
            if len(candidates) > 1 and action != "delete_events":
                conversation["state"] = ConversationState.WAITING_FOR_CLARIFICATION
                conversation["draft"]["candidates"] = candidates
                save_conversation(context, conversation)
                if action == "update_event":
                    conversation["draft"]["selected_candidate_indexes"] = []
                    save_conversation(context, conversation)
                    await update.message.reply_text(
                        "Нашла несколько вариантов:\n\n"
                        + format_candidates(candidates)
                        + "\n\nМожно отметить несколько связанных событий "
                        "(например, дорогу туда, встречу и дорогу обратно), "
                        "затем нажать «Готово»:",
                        reply_markup=multi_event_selection_keyboard(candidates),
                    )
                else:
                    await update.message.reply_text(
                        "Нашла несколько вариантов:\n\n"
                        + format_candidates(candidates)
                        + "\n\nВыбери нужное событие:",
                        reply_markup=selection_keyboard(
                            candidates, "event", destructive=True
                        ),
                    )
                return
            if action == "delete_events":
                draft = conversation["draft"]
                draft["targets"] = candidates
                draft["target"] = candidates[0]
                draft["target_event_id"] = candidates[0]["id"]
                save_conversation(context, conversation)
                await update.message.reply_text(
                    "Удалить все найденные события:\n\n"
                    + format_events(candidates)
                    + "\n\nПодтвердить?",
                    reply_markup=confirmation_keyboard(),
                )
                return
            target = candidates[0]

        draft = conversation["draft"]
        draft["target"] = target
        draft["target_event_id"] = target["id"]
        if action == "update_event":
            conflicts = await asyncio.to_thread(
                find_conflicts,
                draft["events"],
                None,
                {target["id"]},
            )
            draft["conflicts"] = conflicts
            preview = (
                "Изменить событие:\n\n"
                + format_events([target])
                + "\n\nНа:\n\n"
                + format_events(draft["events"])
            )
            warning = format_conflicts(conflicts)
            if warning:
                preview += "\n\n" + warning
        else:
            preview = "Удалить событие:\n\n" + format_events([target])

        save_conversation(context, conversation)
        await update.message.reply_text(
            preview + "\n\nПодтвердить?",
            reply_markup=confirmation_keyboard(),
        )
        return

    if conversation["state"] != ConversationState.IDLE:
        # A model classification error must not silently destroy an active plan.
        save_conversation(context, conversation)
    else:
        clear_conversation(context)

    memories = await asyncio.to_thread(memory_store.as_prompt_context)
    reply = await asyncio.to_thread(get_chat_reply, user_text, memories)
    await update.message.reply_text(reply)


async def handle_error(update, context):
    logger.exception("Ошибка при обработке Telegram update", exc_info=context.error)
    message = getattr(update, "effective_message", None)
    user = getattr(update, "effective_user", None)
    if (
        message
        and ALLOWED_USER_ID is not None
        and user
        and user.id == ALLOWED_USER_ID
    ):
        try:
            if isinstance(context.error, CalendarAuthorizationError):
                text = (
                    "Google Calendar временно отключился: доступ истёк или "
                    "был отозван. Нужно один раз переподключить календарь. "
                    "Твои данные и текущий запрос сохранены."
                )
            else:
                text = (
                    "Я не смогла обработать этот запрос до конца. Ничего не "
                    "выполнила и сохранила контекст — попробуй повторить или "
                    "уточнить формулировку."
                )
            await message.reply_text(text)
        except Exception:
            logger.exception("Не удалось сообщить пользователю об ошибке")


async def handle_unrecognized_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await authorize_update(update):
        return
    message = update.effective_message
    logger.info(
        "Unhandled Telegram message payload: %r",
        message.to_dict() if message else None,
    )
    rich_text = extract_rich_message_text(message) if message else ""
    if rich_text:
        context.user_data["last_structured_input"] = rich_text
        await message.reply_text("🔎 Читаю пересланную таблицу...")
        plan = await asyncio.to_thread(parse_markdown_shifts, rich_text)
        if plan is not None:
            await present_universal_plan(
                update,
                context,
                plan,
                rich_text,
                "Добавить смены Марго из пересланного расписания",
            )
        else:
            await process_universal_payload(
                update,
                context,
                InputPayload("forwarded_message", rich_text),
                "Проанализируй пересланные структурированные данные",
            )
        return
    if message:
        await message.reply_text(
            "Я получила сообщение, но Telegram передал его в формате, который "
            "я пока не умею читать. Отправь его отдельным фото, документом или "
            "обычной пересылкой — не через Reply/цитату."
        )
    if (
        isinstance(update, Update)
        and update.effective_message
        and ALLOWED_USER_ID is not None
        and update.effective_user
        and update.effective_user.id == ALLOWED_USER_ID
    ):
        await update.effective_message.reply_text(
            "Что-то пошло не так, но я сохранила наш разговор. "
            "Попробуйте повторить последнее сообщение."
        )


def main():
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # httpx logs full Telegram request URLs, which include the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if ALLOWED_USER_ID is None:
        logger.warning(
            "TELEGRAM_ALLOWED_USER_ID не настроен: включён безопасный режим"
        )
    PERSISTENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(start_background_tasks)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(
        MessageHandler(filters.ATTACHMENT & ~filters.VOICE, handle_attachment)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.add_handler(MessageHandler(filters.ALL, handle_unrecognized_message))
    app.add_error_handler(handle_error)

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

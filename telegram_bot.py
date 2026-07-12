import asyncio
import logging
import os
import re
from datetime import datetime, timezone
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
from services.memory_service import MemoryStore
from services.voice_service import VoiceQuotaError, transcribe_voice
from services.reminder_service import ReminderStore
from services.action_executor import (
    execute_batch,
    execute_plan,
    format_plan,
)
from services.batch_service import (
    analyze_batch,
    batch_counts,
    format_batch_report,
    format_conflict,
)
from services.extraction_service import extract_content
from services.input_service import InputPayload, detect_message_input
from services.input_dedup_service import InputDedupStore
from services.markdown_schedule_service import (
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
action_history_store = ActionHistoryStore(ACTION_HISTORY_PATH)
reminder_store = ReminderStore(REMINDER_PATH)
input_dedup_store = InputDedupStore(INPUT_DEDUP_PATH)
logger = logging.getLogger(__name__)
LOCAL_TIMEZONE = ZoneInfo("Europe/Podgorica")

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
        rows.append([
            InlineKeyboardButton(
                f"{icon} {title[:48]}",
                callback_data=f"select:{kind}:{index}",
            )
        ])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


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


def conflict_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Оставить существующее", callback_data="batch:keep")],
        [InlineKeyboardButton("🔁 Заменить новым", callback_data="batch:replace")],
        [InlineKeyboardButton("➕ Оставить оба", callback_data="batch:both")],
        [InlineKeyboardButton("✏️ Изменить новое", callback_data="batch:edit")],
        [InlineKeyboardButton("❌ Отмена", callback_data="batch:cancel")],
    ])


async def reminder_dispatcher(application):
    reminder_store.recover_interrupted()
    while True:
        for reminder in reminder_store.claim_due():
            try:
                await application.bot.send_message(
                    chat_id=reminder["user_id"],
                    text="🔔 Напоминаю: " + reminder["text"],
                )
            except Exception:
                reminder_store.release(reminder["id"])
                logger.exception("Не удалось отправить напоминание id=%s", reminder["id"])
            else:
                reminder_store.mark_sent(reminder["id"])
        await asyncio.sleep(20)


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
    create_events(
        payload["events"],
        f"undo{action['id']}{uuid4().hex}",
    )
    return f"Восстановила событий: {len(payload['events'])}."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize_update(update):
        return
    user_text = update.message.text.strip()
    if not user_text:
        return
    if looks_like_schedule(user_text):
        message_key = f"forwarded:{update.effective_chat.id}:{update.message.message_id}"
        if not await asyncio.to_thread(
            input_dedup_store.claim,
            message_key,
            user_text.encode("utf-8"),
        ):
            await update.message.reply_text(
                "Это расписание я уже недавно обработала. Используй готовый план выше."
            )
            return
        await update.message.reply_text("🔎 Читаю таблицу и ищу смены Марго...")
        plan = await asyncio.to_thread(parse_markdown_shifts, user_text)
        await present_universal_plan(update, context, plan, user_text, "Расписание Валеры")
        return
    await process_user_text(update, context, user_text)


async def process_universal_payload(update, context, payload, user_request=""):
    await update.effective_message.reply_text("🔎 Читаю и составляю план...")
    extracted = await asyncio.to_thread(extract_content, payload)
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
        if item["classification"] == "conflict" and item["decision"] is None
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
        format_conflict(draft["plan"], current)
        + f"\n\nОсталось разобрать конфликтов: {len(unresolved)}",
        reply_markup=conflict_keyboard(),
    )


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize_update(update):
        return
    message = update.effective_message
    source_type = detect_message_input(message)
    if source_type == "document":
        await message.reply_text(
            "Пока я читаю изображения, PDF, CSV и XLSX. Этот формат не поддерживается."
        )
        return
    if source_type == "image" and message.photo:
        media = message.photo[-1]
        telegram_file = await context.bot.get_file(media.file_id)
        file_id = media.file_unique_id or media.file_id
        filename = "image.jpg"
        mime_type = "image/jpeg"
    else:
        document = message.document
        telegram_file = await context.bot.get_file(document.file_id)
        file_id = document.file_unique_id or document.file_id
        filename = document.file_name or "document"
        mime_type = document.mime_type or "application/octet-stream"
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
    await process_universal_payload(update, context, payload, message.caption or "")


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
        draft = conversation["draft"]
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
            summary = {
                "created": sum(item["status"] == "done" for item in results),
                "skipped": sum(item["status"] == "skipped_duplicate" for item in results),
                "replaced": 0,
                "cancelled": 0,
            }
        clear_conversation(context)
        await query.message.reply_text(
            "Готово. Итог batch:\n"
            f"Создано: {summary['created']}\n"
            f"Пропущено: {summary['skipped']}\n"
            f"Заменено: {summary['replaced']}\n"
            f"Отменено: {summary['cancelled']}"
        )
        return
    if data == "plan:edit":
        if not conversation or conversation.get("draft", {}).get("operation") != "universal_plan":
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
                        summary = {
                            "created": sum(item["status"] == "done" for item in results),
                            "skipped": sum(item["status"] == "skipped_duplicate" for item in results),
                            "replaced": 0,
                            "cancelled": 0,
                        }
                    clear_conversation(context)
                    await update.message.reply_text(
                        "Готово. Итог batch:\n"
                        f"Создано: {summary['created']}\n"
                        f"Пропущено: {summary['skipped']}\n"
                        f"Заменено: {summary['replaced']}\n"
                        f"Отменено: {summary['cancelled']}"
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

                if operation in {"create_events", "update_event"}:
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
    memories = await asyncio.to_thread(memory_store.as_prompt_context)
    intent = await asyncio.to_thread(
        detect_intent,
        user_text,
        conversation,
        memories,
    )
    await asyncio.to_thread(
        memory_store.apply_updates,
        intent.get("memory_updates", []),
    )

    action = intent.get("action")

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
                search_events,
                intent.get("search", {}),
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
                await update.message.reply_text(
                    "Нашла несколько вариантов:\n\n"
                    + format_candidates(candidates)
                    + "\n\nВыбери нужное событие:",
                    reply_markup=selection_keyboard(
                        candidates,
                        "event",
                        destructive=action != "update_event",
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
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, handle_attachment)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(handle_error)

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

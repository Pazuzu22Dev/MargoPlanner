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
memory_store = MemoryStore(MEMORY_PATH)
action_history_store = ActionHistoryStore(ACTION_HISTORY_PATH)
reminder_store = ReminderStore(REMINDER_PATH)
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
    await process_user_text(update, context, user_text)


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
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(handle_error)

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

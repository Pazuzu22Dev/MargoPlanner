import os
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from services.calendar_service import create_event
from services.chat_service import get_chat_reply
from services.conversation_service import (
    ConversationState,
    clear_conversation,
    get_conversation,
    save_conversation,
)
from services.intent_service import detect_intent


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в .env")


YES_ANSWERS = {"да", "ага", "создавай", "подтверждаю", "ок", "окей"}
NO_ANSWERS = {"нет", "отмена", "не надо", "отменить"}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Да, моя госпожа. 👋 Я MargoPlanner и готов к работе."
    )


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

    return "\n\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    normalized_text = user_text.lower()

    conversation = get_conversation(context)

    if conversation:
        state = conversation["state"]

        if state == ConversationState.WAITING_FOR_CLARIFICATION:
            combined_text = (
                f"{conversation['original_text']}\n\n"
                f"Уточнение Марго: {user_text}"
            )

            clear_conversation(context)
            intent = detect_intent(combined_text)

        elif state == ConversationState.WAITING_FOR_CONFIRMATION:
            events = conversation["data"]["events"]

            if normalized_text in YES_ANSWERS:
                links = []

                for event_data in events:
                    created_event = create_event(
                        summary=event_data["title"],
                        start_time=event_data["start_time"],
                        end_time=event_data["end_time"],
                    )
                    links.append(created_event.get("htmlLink", ""))

                clear_conversation(context)

                await update.message.reply_text(
                    "Готово. Всё добавлено в календарь. 🗓\n\n"
                    + "\n".join(links)
                )
                return

            if normalized_text in NO_ANSWERS:
                clear_conversation(context)

                await update.message.reply_text(
                    "Отменила. Календарь остался невредим."
                )
                return

            combined_text = (
                f"Первоначальный запрос Марго:\n"
                f"{conversation['original_text']}\n\n"
                f"Пинки предложил события:\n"
                f"{events}\n\n"
                f"Марго исправила или дополнила план:\n"
                f"{user_text}"
            )

            clear_conversation(context)
            intent = detect_intent(combined_text)

        else:
            clear_conversation(context)
            intent = detect_intent(user_text)

    else:
        intent = detect_intent(user_text)

    action = intent.get("action")

    if action == "clarify":
        save_conversation(
            context=context,
            state=ConversationState.WAITING_FOR_CLARIFICATION,
            original_text=user_text,
            data=intent,
        )

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

        save_conversation(
            context=context,
            state=ConversationState.WAITING_FOR_CONFIRMATION,
            original_text=user_text,
            data={"events": events},
        )

        await update.message.reply_text(
            "Я поняла так:\n\n"
            + format_events(events)
            + "\n\nСоздать? Ответьте «да» или «нет»."
        )
        return

    reply = get_chat_reply(user_text)
    await update.message.reply_text(reply)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
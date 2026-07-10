import os

from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в .env")

if not GEMINI_API_KEY:
    raise RuntimeError("Не найден GEMINI_API_KEY в .env")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
with open("PERSONALITY.md", "r", encoding="utf-8") as file:
    SYSTEM_PROMPT = file.read()



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Да, моя госпожа. 👋 Я MargoPlanner и я готов служить."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text

    response = gemini_client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=f"{SYSTEM_PROMPT}\n\nСообщение Марго: {user_text}"
    )

    await update.message.reply_text(response.text)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
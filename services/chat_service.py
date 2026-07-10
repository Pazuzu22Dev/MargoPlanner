import os

from dotenv import load_dotenv
from google import genai


load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("Не найден GEMINI_API_KEY в .env")


gemini_client = genai.Client(api_key=GEMINI_API_KEY)


with open("brain/PERSONALITY.md", "r", encoding="utf-8") as file:
    SYSTEM_PROMPT = file.read()

with open("brain/USER.md", "r", encoding="utf-8") as file:
    USER_INFO = file.read()


def get_chat_reply(user_text: str) -> str:
    response = gemini_client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=(
            f"{SYSTEM_PROMPT}\n\n"
            f"{USER_INFO}\n\n"
            f"Сообщение Марго:\n{user_text}"
        ),
    )

    return response.text
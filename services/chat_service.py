import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai


load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_CHAT_MODEL = os.getenv(
    "GEMINI_CHAT_MODEL",
    "gemini-3.1-flash-lite",
)

if not GEMINI_API_KEY:
    raise RuntimeError("Не найден GEMINI_API_KEY в .env")


gemini_client = genai.Client(api_key=GEMINI_API_KEY)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


with (PROJECT_ROOT / "brain" / "PERSONALITY.md").open(
    "r", encoding="utf-8"
) as file:
    SYSTEM_PROMPT = file.read()

with (PROJECT_ROOT / "brain" / "USER.md").open(
    "r", encoding="utf-8"
) as file:
    USER_INFO = file.read()


def get_chat_reply(user_text: str, memories="") -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_CHAT_MODEL,
        contents=(
            f"{SYSTEM_PROMPT}\n\n"
            f"{USER_INFO}\n\n"
            f"Долговременная память о Марго:\n{memories or '[]'}\n\n"
            f"Сообщение Марго:\n{user_text}"
        ),
    )

    return response.text

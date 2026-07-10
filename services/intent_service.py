import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("Не найден GEMINI_API_KEY в .env")


gemini_client = genai.Client(api_key=GEMINI_API_KEY)

TIMEZONE = ZoneInfo("Europe/Podgorica")


def detect_intent(user_text: str) -> dict:
    current_time = datetime.now(TIMEZONE).isoformat()

    prompt = f"""
Сейчас: {current_time}
Часовой пояс пользователя: Europe/Podgorica.

Ты анализируешь живую, неструктурированную речь Марго.
Она может думать вслух, путать даты, менять решение по ходу фразы
и описывать несколько связанных действий сразу.

Верни строго JSON.

Возможные action:
- "chat" — обычный разговор;
- "clarify" — нужно уточнение;
- "create_events" — можно подготовить одно или несколько событий.

Формат ответа:

{{
  "action": "chat | clarify | create_events",
  "clarification_question": "вопрос пользователю или пустая строка",
  "reason": "кратко, почему нужно уточнение или что было понято",
  "events": [
    {{
      "title": "название события",
      "start_time": "ISO 8601 или пустая строка",
      "end_time": "ISO 8601 или пустая строка"
    }}
  ]
}}

Правила:

1. Проверяй относительные даты по текущей дате.
2. Если пользователь говорит что-то вроде:
   "через 3 дня, вроде это 16-е"
   и эти данные противоречат друг другу,
   не угадывай — верни action="clarify".
3. Если не хватает времени начала, даты или другого важного параметра,
   верни action="clarify".
4. Если указана длительность события, вычисли end_time.
5. Если указана дорога до события,
   создай отдельное событие для выезда.
6. Если дорога длится 2 часа, а событие начинается в 15:00,
   событие выезда должно начинаться в 13:00 и заканчиваться в 15:00.
7. Если длительность основного события не указана,
   используй 1 час.
8. Не создавай события сам. Только анализируй.
9. Для обычного разговора events должен быть пустым списком.

Сообщение Марго:
{user_text}
"""
    
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )

            return json.loads(response.text)

        except ServerError as error:
            if attempt == 2:
                raise

            wait_seconds = 2 ** attempt
            print(
                f"Gemini временно недоступен. "
                f"Повтор через {wait_seconds} сек."
            )
            time.sleep(wait_seconds)
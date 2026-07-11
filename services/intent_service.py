import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError

from services.memory_service import validate_memory_updates

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_BRAIN_MODEL = os.getenv(
    "GEMINI_BRAIN_MODEL",
    "gemini-3.1-flash-lite",
)

if not GEMINI_API_KEY:
    raise RuntimeError("Не найден GEMINI_API_KEY в .env")


gemini_client = genai.Client(api_key=GEMINI_API_KEY)
TIMEZONE = ZoneInfo("Europe/Podgorica")
ALLOWED_ACTIONS = {
    "chat",
    "clarify",
    "create_events",
    "update_event",
    "delete_event",
    "delete_events",
    "create_reminder",
    "list_reminders",
}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_iso_datetime(value, field_name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} должен содержать дату и время")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} должен содержать часовой пояс")
    return parsed


def _string_list(event, field_name):
    value = event.get(field_name) or []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} должен быть списком")
    return [str(item).strip() for item in value if str(item).strip()]


def validate_intent(raw_intent):
    """Validate the model boundary before its output can reach an API."""
    if not isinstance(raw_intent, dict):
        raise ValueError("Gemini должен вернуть JSON-объект")

    action = raw_intent.get("action")
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Неизвестное действие: {action}")

    result = {
        "action": action,
        "clarification_question": str(
            raw_intent.get("clarification_question", "")
        ).strip(),
        "reason": str(raw_intent.get("reason", "")).strip(),
        "events": raw_intent.get("events") or [],
        "target_event_id": str(raw_intent.get("target_event_id", "")).strip(),
        "search": raw_intent.get("search") or {},
        "memory_updates": validate_memory_updates(
            raw_intent.get("memory_updates", [])
        ),
        "reminder": raw_intent.get("reminder") or {},
    }

    if not isinstance(result["events"], list):
        raise ValueError("events должен быть списком")
    if not isinstance(result["search"], dict):
        raise ValueError("search должен быть объектом")
    if not isinstance(result["reminder"], dict):
        raise ValueError("reminder должен быть объектом")
    if action == "clarify" and not result["clarification_question"]:
        raise ValueError("Для уточнения нужен вопрос")
    if action == "chat":
        result["events"] = []
    if action in {"delete_event", "delete_events"}:
        result["events"] = []
    if action == "create_events" and not result["events"]:
        raise ValueError("Для создания нужен хотя бы один event")
    if action == "create_reminder":
        reminder_text = str(result["reminder"].get("text", "")).strip()
        remind_at = _validate_iso_datetime(
            result["reminder"].get("remind_at"), "remind_at"
        )
        if not reminder_text:
            raise ValueError("У напоминания должен быть текст")
        result["reminder"] = {
            "text": reminder_text,
            "remind_at": remind_at.isoformat(),
        }
        result["events"] = []
    if action == "update_event" and len(result["events"]) != 1:
        raise ValueError("Для изменения нужен ровно один новый вариант события")
    if action in {"update_event", "delete_event", "delete_events", "list_reminders"}:
        result["search"] = {
            "text": str(result["search"].get("text", "")).strip(),
            "time_min": str(result["search"].get("time_min", "")).strip(),
            "time_max": str(result["search"].get("time_max", "")).strip(),
        }
        if action == "list_reminders":
            if not result["search"]["time_min"] or not result["search"]["time_max"]:
                raise ValueError("Для списка напоминаний нужен диапазон дат")
            _validate_iso_datetime(result["search"]["time_min"], "time_min")
            _validate_iso_datetime(result["search"]["time_max"], "time_max")
        elif not result["target_event_id"] and not any(result["search"].values()):
            raise ValueError("Для изменения или удаления нужны данные поиска")

    normalized_events = []
    for event in result["events"]:
        if not isinstance(event, dict):
            raise ValueError("Каждое событие должно быть объектом")
        title = str(event.get("title", "")).strip()
        if not title:
            raise ValueError("У события должно быть название")
        start = _validate_iso_datetime(event.get("start_time"), "start_time")
        end = _validate_iso_datetime(event.get("end_time"), "end_time")
        if end <= start:
            raise ValueError("Конец события должен быть позже начала")
        normalized_events.append(
            {
                "title": title,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "description": str(event.get("description", "")).strip(),
                "location": str(event.get("location", "")).strip(),
                "links": _string_list(event, "links"),
                "contacts": _string_list(event, "contacts"),
                "attendees": [
                    email.lower()
                    for email in _string_list(event, "attendees")
                    if EMAIL_PATTERN.fullmatch(email)
                ],
            }
        )

    if action not in {"chat", "delete_event", "delete_events"}:
        result["events"] = normalized_events
    return result


def detect_intent(user_text: str, conversation=None, memories="") -> dict:
    current_time = datetime.now(TIMEZONE).isoformat()
    conversation_json = json.dumps(
        conversation or {}, ensure_ascii=False, indent=2, default=str
    )

    prompt = f"""
Сейчас: {current_time}
Часовой пояс Марго: Europe/Podgorica.

Ты — мозг личного помощника Пинки. Анализируй не отдельную команду, а
продолжающийся человеческий разговор. Марго может думать вслух, исправлять
себя и постепенно добавлять детали.

Текущее состояние разговора:
{conversation_json}

Долговременная память (может быть пустой):
{memories or "[]"}

Новое сообщение Марго:
{user_text}

Верни строго JSON с полной актуальной версией плана:
{{
  "action": "chat | clarify | create_events | update_event | delete_event | delete_events | create_reminder | list_reminders",
  "clarification_question": "один естественный вопрос или пустая строка",
  "reason": "кратко, что понято",
  "target_event_id": "ID только из draft.candidates или пустая строка",
  "search": {{
    "text": "слова из названия искомого события",
    "time_min": "начало диапазона ISO 8601 или пустая строка",
    "time_max": "конец диапазона ISO 8601 или пустая строка"
  }},
  "memory_updates": [
    {{
      "operation": "set | delete",
      "category": "person | place | project | preference",
      "key": "короткий уникальный ключ",
      "value": "полный актуальный факт; для delete пустая строка"
    }}
  ],
  "reminder": {{
    "text": "что именно напомнить или пустая строка",
    "remind_at": "ISO 8601 с часовым поясом или пустая строка"
  }},
  "events": [
    {{
      "title": "название события",
      "start_time": "ISO 8601 с часовым поясом",
      "end_time": "ISO 8601 с часовым поясом",
      "description": "дополнительные детали или пустая строка",
      "location": "адрес/место или пустая строка",
      "links": ["ссылки из сообщения"],
      "contacts": ["телефон, email, Telegram или другой способ связи"],
      "attendees": ["email только для явно приглашённых участников"]
    }}
  ]
}}

Правила:
1. Учитывай всю историю, draft и исправления. Новое явное исправление важнее
   старой информации.
2. events — полная актуальная версия всех связанных событий, а не только
   изменения последнего сообщения.
3. Если есть противоречие или не хватает обязательной даты/времени, верни
   clarify. Задавай только один самый полезный вопрос за раз.
4. При clarify сохрани в events уже известные события, только если у них есть
   полноценные start_time и end_time. Не выдумывай недостающие даты.
5. Если длительность основной встречи не дана, используй 1 час.
6. Дорогу и подготовку создавай отдельными событиями и пересчитывай их при
   переносе основной встречи.
7. create_events означает, что план полон и его можно показать Марго для
   подтверждения. Сам ничего не создавай.
8. update_event используй для переноса или редактирования. В events верни
   ровно одну полную новую версию события. Старое событие опиши через search.
9. delete_event используй для удаления. events оставь пустым, старое событие
   опиши через search.
10. delete_events используй, когда Марго явно просит удалить все события в
    указанном диапазоне. Если названа только дата, search.time_min поставь на
    00:00 этой даты, search.time_max — на 00:00 следующего дня, search.text
    оставь пустым. Никогда не заменяй указанную дату текущей датой.
11. Никогда не выдумывай target_event_id. Используй ID только если он уже есть
    среди candidates в состоянии разговора и Марго однозначно выбрала вариант.
12. chat используй только для разговора, не связанного с активным планом.
13. В memory_updates добавляй только устойчивые факты, которые Марго сказала
    явно: людей, постоянные места, проекты, привычки и предпочтения. Не сохраняй
    догадки, разовые планы, содержание календарных событий, пароли, токены и
    секреты. Исправление прежнего факта возвращай как set с тем же key и новым
    полным value. Просьбу забыть факт возвращай как delete.
14. Всегда сохраняй явно указанные ссылки в links, способы связи в contacts,
    адрес в location. Email в contacts не означает приглашение.
15. Добавляй email в attendees только если Марго явно просит пригласить человека
    или добавить его участником события. Не отправляй приглашения по догадке.
16. Если Марго говорит «напомни» и это личное напоминание, используй
    create_reminder, а не Google Calendar. Заполни reminder.text и remind_at.
    Если точной даты или времени нет — clarify. Фразы «часов в 10» понимай как
    приблизительное 10:00, если это не создаёт противоречия.
17. Если Марго спрашивает, какие напоминания у неё есть, используй
    list_reminders. В search.time_min и search.time_max верни границы нужного
    дня или периода с часовым поясом. Для «сегодня» это 00:00 сегодняшнего дня
    и 00:00 следующего; для «завтра» — аналогично для завтрашнего дня.
    search.text оставь пустым. Это запрос списка, подтверждение не требуется.
"""

    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_BRAIN_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            return validate_intent(json.loads(response.text))
        except ServerError:
            if attempt == 2:
                raise
            wait_seconds = 2 ** attempt
            print(
                f"Gemini временно недоступен. "
                f"Повтор через {wait_seconds} сек."
            )
            time.sleep(wait_seconds)

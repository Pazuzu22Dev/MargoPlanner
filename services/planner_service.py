import json
from datetime import datetime
from zoneinfo import ZoneInfo

from google.genai import types

from services.intent_service import GEMINI_BRAIN_MODEL, gemini_client


STANDARD_ACTIONS = {
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "create_reminder",
    "update_reminder",
    "delete_reminder",
}
TIMEZONE = ZoneInfo("Europe/Podgorica")


def validate_plan(raw):
    if not isinstance(raw, dict):
        raise ValueError("План должен быть JSON-объектом")
    question = str(raw.get("clarification_question", "")).strip()
    actions = raw.get("actions") or []
    if not isinstance(actions, list):
        raise ValueError("actions должен быть списком")
    normalized = []
    for item in actions:
        if not isinstance(item, dict) or item.get("action") not in STANDARD_ACTIONS:
            raise ValueError("План содержит запрещённое действие")
        action = item["action"]
        data = item.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("data должен быть объектом")
        if action in {"create_calendar_event", "update_calendar_event"}:
            for field in ("title", "start_time", "end_time"):
                if not data.get(field):
                    raise ValueError(f"У события отсутствует {field}")
            start = datetime.fromisoformat(data["start_time"])
            end = datetime.fromisoformat(data["end_time"])
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise ValueError("Некорректное время события")
        if action in {"create_reminder", "update_reminder"}:
            if not data.get("text") or not data.get("remind_at"):
                raise ValueError("У напоминания нужны текст и время")
            if datetime.fromisoformat(data["remind_at"]).tzinfo is None:
                raise ValueError("У времени напоминания нет часового пояса")
        if action.startswith("update_") and not data.get("id"):
            raise ValueError("Для изменения нужен id")
        if action.startswith("delete_") and not (data.get("id") or data.get("ids")):
            raise ValueError("Для удаления нужен id")
        normalized.append({"action": action, "data": data})
    if not normalized and not question:
        raise ValueError("Пустой план")
    return {"actions": normalized, "clarification_question": question, "notes": raw.get("notes", [])}


def build_plan(extracted, user_request="", memories=""):
    now = datetime.now(TIMEZONE).isoformat()
    prompt = f"""
Ты — универсальный AI-планировщик MargoPlanner. Сейчас {now}, часовой пояс Europe/Podgorica.
Запрос пользователя: {user_request or 'Определи полезные действия из содержимого'}
Память: {memories or '[]'}

Верни строго JSON:
{{"actions":[{{"action":"create_calendar_event | update_calendar_event | delete_calendar_event | create_reminder | update_reminder | delete_reminder","data":{{}}}}],"clarification_question":"","notes":[]}}

Разрешены только перечисленные actions. Ничего не выполняй. Если данных недостаточно — actions оставь пустым и задай один вопрос.
Для create_calendar_event обязательны title, start_time, end_time; дополнительно description, location, links, contacts, attendees.
Для смен из таблицы самостоятельно найди колонки сотрудника, даты, начала и окончания, выбери только строки Марго и назови события «Рабочая смена».
Не создавай действия для других сотрудников. Все даты верни ISO 8601 с часовым поясом.
Содержимое:
"""
    contents = [prompt]
    if isinstance(extracted, dict) and "image" in extracted:
        contents.append(types.Part.from_bytes(data=extracted["image"], mime_type=extracted["mime_type"] or "image/jpeg"))
        contents.append(extracted.get("caption", ""))
    else:
        contents.append(str(extracted))
    response = gemini_client.models.generate_content(
        model=GEMINI_BRAIN_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return validate_plan(json.loads(response.text))

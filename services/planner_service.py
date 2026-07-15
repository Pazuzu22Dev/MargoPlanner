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
    seen_events = set()
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
            if str(data.get("row_type", "")).strip().casefold() in {
                "выходной", "off", "day off",
            }:
                continue
            employee = str(data.get("employee", "")).strip()
            if employee and "марго" not in employee.casefold():
                continue
            key = (data.get("title"), start.isoformat(), end.isoformat())
            if action == "create_calendar_event" and key in seen_events:
                continue
            seen_events.add(key)
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
        question = (
            "Я не нашла подходящих действий или строк Марго. "
            "Уточни запрос или пришли более чёткий файл."
        )
    notes = raw.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list):
        notes = [str(notes)]
    return {
        "actions": normalized,
        "clarification_question": question,
        "notes": [str(note).strip() for note in notes if str(note).strip()],
    }


def build_plan(extracted, user_request="", memories=""):
    now = datetime.now(TIMEZONE).isoformat()
    prompt = f"""
Ты — универсальный AI-планировщик MargoPlanner. Сейчас {now}, часовой пояс Europe/Podgorica.
Запрос пользователя: {user_request or 'Определи полезные действия из содержимого'}
Память: {memories or '[]'}

Содержимое ниже уже непосредственно получено из Telegram-сообщения пользователя.
Не проси доступ к таблице, локальному файлу или ссылке и не проси копировать данные,
если текст или изображение присутствуют в «Содержимое». Анализируй переданные данные.
Если текст действительно неполный, перечисли, какую часть удалось прочитать, и
задай конкретный вопрос только о недостающих значениях.

Верни строго JSON:
{{"actions":[{{"action":"create_calendar_event | update_calendar_event | delete_calendar_event | create_reminder | update_reminder | delete_reminder","data":{{}}}}],"clarification_question":"","notes":[]}}

Разрешены только перечисленные actions. Ничего не выполняй. Если данных недостаточно — actions оставь пустым и задай один вопрос.
Извлеки ВСЕ действия из запроса, а не только первое или главное. Каждую
отдельную просьбу верни отдельным элементом actions и сохрани порядок просьб.
Например, «добавь событие с 11 до 12 и сделай напоминание о нём в 9» должно
дать два действия: create_calendar_event и create_reminder. Не превращай
напоминание в уведомление Google Calendar и не теряй его после создания события.
Если напоминание ссылается на событие словами «о нём/об этом», используй дату
события, время напоминания из запроса и понятный текст с названием события.
Для create_calendar_event обязательны title, start_time, end_time; дополнительно description, location, links, contacts, attendees.
Для смен из таблицы самостоятельно найди колонки сотрудника, даты, начала и окончания. Выбери только строки, где сотрудник — Марго, и назови события «Рабочая смена».
В data каждой смены обязательно добавь employee с именем из исходной строки и row_type с типом строки.
Не включай других сотрудников и строки «Выходной». Не угадывай конец смены: если хотя бы у одной смены Марго не виден конец, верни пустой actions и clarification_question.
Если изображение обрезано и видна не вся таблица, добавь понятное предупреждение в notes. В notes также сообщи о сомнительных или плохо читаемых строках.
Проверь каждую видимую строку Марго по отдельности и не повторяй одинаковые смены. Все даты верни ISO 8601 с часовым поясом.
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
    raw = json.loads(response.text)
    for item in raw.get("actions") or []:
        if item.get("action") in {"create_calendar_event", "update_calendar_event"}:
            data = item.get("data") or {}
            if not data.get("end_time"):
                return {
                    "actions": [],
                    "clarification_question": (
                        "Я вижу смену без времени окончания. Подскажи, "
                        "во сколько она заканчивается?"
                    ),
                    "notes": raw.get("notes", []),
                }
            if "смен" in str(data.get("title", "")).casefold() and not data.get("employee"):
                return {
                    "actions": [],
                    "clarification_question": (
                        "Я не смогла уверенно прочитать имя сотрудника в одной "
                        "из строк. Пришли, пожалуйста, более чёткий или полный снимок."
                    ),
                    "notes": raw.get("notes", []),
                }
    return validate_plan(raw)

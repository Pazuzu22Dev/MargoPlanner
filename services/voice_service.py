import json
import os
import re

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError


load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
VOICE_MODEL = os.getenv("GEMINI_VOICE_MODEL", "gemini-3.1-flash-lite")
VOICE_FALLBACK_MODEL = "gemini-2.5-flash"

if not GEMINI_API_KEY:
    raise RuntimeError("Не найден GEMINI_API_KEY в .env")


gemini_client = genai.Client(api_key=GEMINI_API_KEY)


class VoiceQuotaError(RuntimeError):
    def __init__(self, retry_after_seconds=None):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Исчерпана квота распознавания голоса")


def _retry_after_seconds(error):
    match = re.search(r"retry in ([0-9.]+)s", str(error), flags=re.IGNORECASE)
    if not match:
        match = re.search(r"'retryDelay': '([0-9]+)s'", str(error))
    return max(1, round(float(match.group(1)))) if match else None


def transcribe_voice(audio_bytes, mime_type="audio/ogg"):
    if not audio_bytes:
        raise ValueError("Голосовое сообщение пустое")

    contents = [
        (
            "Точно расшифруй голосовое сообщение Марго на языке "
            "оригинала. Сохрани даты, время, имена и "
            "паузы-самоисправления вроде «нет», «хотя» и «лучше». "
            "Не отвечай на сообщение и ничего не добавляй от себя. "
            "Верни только JSON."
        ),
        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
    ]
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema={
            "type": "object",
            "properties": {"transcript": {"type": "string"}},
            "required": ["transcript"],
        },
    )
    models = list(dict.fromkeys([VOICE_MODEL, VOICE_FALLBACK_MODEL]))
    response = None
    last_error = None
    for model in models:
        try:
            response = gemini_client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            break
        except ClientError as error:
            last_error = error
            if error.code == 404:
                continue
            if error.code == 429:
                raise VoiceQuotaError(_retry_after_seconds(error)) from error
            raise
    if response is None:
        raise last_error
    result = json.loads(response.text)
    transcript = str(result.get("transcript", "")).strip()
    if not transcript:
        raise ValueError("Не удалось распознать речь")
    return transcript

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-key")

from services.voice_service import VoiceQuotaError, transcribe_voice
from google.genai.errors import ClientError


class VoiceServiceTests(unittest.TestCase):
    def test_transcription_returns_clean_text(self):
        response = type(
            "Response",
            (),
            {"text": json.dumps({"transcript": "  Лучше в пятницу.  "})},
        )()
        with patch(
            "services.voice_service.gemini_client.models.generate_content",
            return_value=response,
        ) as generate_mock:
            transcript = transcribe_voice(b"audio", "audio/ogg")
        self.assertEqual(transcript, "Лучше в пятницу.")
        self.assertEqual(
            generate_mock.call_args.kwargs["contents"][1].inline_data.mime_type,
            "audio/ogg",
        )

    def test_empty_audio_is_rejected_before_api_call(self):
        with self.assertRaises(ValueError):
            transcribe_voice(b"")

    def test_quota_error_has_retry_time(self):
        error = ClientError(
            429,
            {"error": {"message": "Please retry in 12.4s."}},
            None,
        )
        with patch(
            "services.voice_service.gemini_client.models.generate_content",
            side_effect=error,
        ):
            with self.assertRaises(VoiceQuotaError) as raised:
                transcribe_voice(b"audio")
        self.assertEqual(raised.exception.retry_after_seconds, 12)


if __name__ == "__main__":
    unittest.main()

# MargoPlanner

Личный AI-помощник Марго: понимает естественный разговор, уточняет детали и
создаёт связанные события в Google Calendar после подтверждения. Текстовые и
голосовые сообщения проходят через один и тот же диалоговый контекст.

## Требования

- Python 3.11–3.13
- Telegram Bot Token
- Gemini API key
- Google Calendar OAuth credentials

## Локальный запуск

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python telegram_bot.py
```

В `.env` должны находиться:

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_ID=...
GEMINI_API_KEY=...
```

### Первоначальная защита Telegram

Если `TELEGRAM_ALLOWED_USER_ID` ещё не указан, запустите бота и отправьте ему
любое сообщение. В безопасном режиме он покажет ваш числовой ID, но не получит
доступ к Gemini, памяти или календарю. Добавьте показанный ID в `.env` и
перезапустите бота. После этого сообщения остальных пользователей будут
игнорироваться.

Файлы `credentials.json`, `token.json`, `.env` и сохранённые разговоры не
попадают в Git.

## Railway 24/7

Проект готов к запуску как постоянный Railway service через `Dockerfile`.

1. Создайте Railway-проект и сервис из этого репозитория.
2. Добавьте persistent volume с mount path `/data`.
3. Добавьте service variables:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_USER_ID
GEMINI_API_KEY
MARGOPLANNER_DATA_DIR=/data
GOOGLE_CREDENTIALS_B64
GOOGLE_TOKEN_B64
```

Получить base64 без изменения исходных OAuth-файлов на macOS:

```bash
base64 < credentials.json | tr -d '\n'
base64 < token.json | tr -d '\n'
```

Первый результат сохраните как `GOOGLE_CREDENTIALS_B64`, второй — как
`GOOGLE_TOKEN_B64`. Эти значения являются секретами: не добавляйте их в Git и
не отправляйте в переписке.

После успешного запуска Railway остановите локальную копию: одновременно
работать с одним Telegram token через polling должен только один процесс.

## Тесты

```bash
python -m unittest \
  tests.test_conversation_service \
  tests.test_intent_validation \
  tests.test_calendar_service_unit \
  tests.test_telegram_flow \
  tests.test_memory_service \
  tests.test_voice_service \
  tests.test_action_history_service
```

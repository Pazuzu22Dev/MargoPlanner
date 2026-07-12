from dataclasses import dataclass


@dataclass(frozen=True)
class InputPayload:
    source_type: str
    content: bytes | str
    filename: str = ""
    mime_type: str = ""
    caption: str = ""


@dataclass(frozen=True)
class NormalizedTelegramInput:
    main_text: str
    caption: str
    reply_text: str
    reply_caption: str
    quote_text: str
    combined_text: str
    source_type: str
    is_forwarded: bool
    has_photo: bool
    has_document: bool
    has_voice: bool
    has_external_reply: bool
    attachment_message: object | None = None


def _rich_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_rich_text(item) for item in value)
    if isinstance(value, dict):
        return _rich_text(value.get("text", ""))
    return str(value or "")


def extract_rich_message_text(message):
    rich_message = getattr(message, "rich_message", None)
    if not rich_message:
        rich_message = (getattr(message, "api_kwargs", None) or {}).get(
            "rich_message"
        )
    if not isinstance(rich_message, dict):
        return ""
    output = []
    for block in rich_message.get("blocks") or []:
        block_type = block.get("type")
        if block_type in {"paragraph", "heading"}:
            text = _rich_text(block.get("text")).strip()
            if text:
                output.append(text)
        elif block_type == "table":
            rows = block.get("cells") or []
            rendered_rows = []
            for row in rows:
                rendered_rows.append(
                    "| " + " | ".join(
                        _rich_text(cell.get("text", "")).strip()
                        for cell in row
                    ) + " |"
                )
            if rendered_rows:
                column_count = len(rows[0])
                rendered_rows.insert(
                    1,
                    "| " + " | ".join("---" for _ in range(column_count)) + " |",
                )
                output.append("\n".join(rendered_rows))
    return "\n\n".join(output)


def normalize_telegram_message(message):
    reply = getattr(message, "reply_to_message", None)
    external_reply = getattr(message, "external_reply", None)
    quote = getattr(message, "quote", None)
    quote_text = (getattr(quote, "text", None) or "").strip()
    main_text = (
        getattr(message, "text", None)
        or extract_rich_message_text(message)
        or ""
    ).strip()
    caption = (getattr(message, "caption", None) or "").strip()
    reply_text = (getattr(reply, "text", None) or "").strip() if reply else ""
    reply_caption = (
        (getattr(reply, "caption", None) or "").strip() if reply else ""
    )
    if reply and get_message_attachment(reply):
        attachment_message = reply
    elif external_reply and get_message_attachment(external_reply):
        attachment_message = external_reply
    else:
        attachment_message = message
    if not get_message_attachment(attachment_message):
        attachment_message = None
    context_text = reply_text or reply_caption or quote_text
    current_text = main_text or caption
    if context_text:
        combined_text = (
            "Контекст сообщения:\n"
            + context_text
            + "\n\nСообщение пользователя:\n"
            + (current_text or "Проанализируй это сообщение")
        )
    else:
        combined_text = current_text
    return NormalizedTelegramInput(
        main_text=main_text,
        caption=caption,
        reply_text=reply_text,
        reply_caption=reply_caption,
        quote_text=quote_text,
        combined_text=combined_text,
        source_type=detect_message_input(attachment_message or message),
        is_forwarded=bool(
            getattr(message, "forward_origin", None) or external_reply
        ),
        has_photo=bool(getattr(attachment_message or message, "photo", None)),
        has_document=bool(getattr(attachment_message or message, "document", None)),
        has_voice=bool(getattr(attachment_message or message, "voice", None)),
        has_external_reply=bool(external_reply),
        attachment_message=attachment_message,
    )


def is_structured_telegram_text(normalized):
    text = normalized.reply_text or normalized.quote_text or normalized.main_text
    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    return bool(text) and (
        normalized.is_forwarded
        or bool(normalized.reply_text or normalized.quote_text)
        or text.count("|") >= 4
        or len(nonempty_lines) >= 3
    )


def detect_message_input(message):
    if getattr(message, "photo", None):
        return "image"
    animation = getattr(message, "animation", None)
    if animation:
        return "image"
    video = getattr(message, "video", None)
    if video:
        return "image"
    sticker = getattr(message, "sticker", None)
    if sticker and not getattr(sticker, "is_animated", False):
        return "image"
    document = getattr(message, "document", None)
    if document:
        mime = (document.mime_type or "").lower()
        name = (document.file_name or "").lower()
        if mime.startswith("image/"):
            return "image"
        if name.endswith(".xlsx"):
            return "xlsx"
        if name.endswith(".csv") or mime == "text/csv":
            return "csv"
        if name.endswith(".pdf") or mime == "application/pdf":
            return "pdf"
        return "document"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "forward_origin", None):
        return "forwarded_message"
    return "text"


def get_message_attachment(message):
    """Return a downloadable Telegram media object and its metadata.

    Telegram may deliver the same-looking forwarded schedule as a photo,
    document, animation, video or sticker. Keeping this normalization here
    prevents the bot routing layer from depending on one Telegram media type.
    """
    photos = getattr(message, "photo", None)
    if photos:
        media = photos[-1]
        return media, "image.jpg", "image/jpeg"

    for attribute, default_name, default_mime in (
        ("document", "document", "application/octet-stream"),
        ("animation", "animation.mp4", "video/mp4"),
        ("video", "video.mp4", "video/mp4"),
        ("sticker", "sticker.webp", "image/webp"),
    ):
        media = getattr(message, attribute, None)
        if not media:
            continue
        filename = getattr(media, "file_name", None) or default_name
        mime_type = getattr(media, "mime_type", None) or default_mime
        return media, filename, mime_type

    return None

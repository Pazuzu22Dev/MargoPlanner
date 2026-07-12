from dataclasses import dataclass


@dataclass(frozen=True)
class InputPayload:
    source_type: str
    content: bytes | str
    filename: str = ""
    mime_type: str = ""
    caption: str = ""


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

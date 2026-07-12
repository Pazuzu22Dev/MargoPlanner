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

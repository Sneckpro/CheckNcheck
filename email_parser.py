import imaplib
import email
from email.header import decode_header
import logging

logger = logging.getLogger(__name__)


def _decode_header(header: str) -> str:
    parts = decode_header(header)
    decoded = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_text(msg: email.message.Message) -> str:
    """Extract text content from email message."""
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    texts.append(payload.decode(charset, errors="replace"))
            elif content_type == "text/html" and not texts:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    texts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            texts.append(payload.decode(charset, errors="replace"))
    return "\n".join(texts)[:3000]  # Limit to 3000 chars for GPT


def fetch_unseen_emails(server: str, address: str, password: str) -> list[dict]:
    """Connect to IMAP, fetch unseen emails, return list of {from, subject, body}."""
    results = []
    try:
        mail = imaplib.IMAP4_SSL(server)
        mail.login(address, password)
        mail.select("INBOX")

        _, message_ids = mail.search(None, "UNSEEN")
        if not message_ids[0]:
            mail.logout()
            return results

        for msg_id in message_ids[0].split()[-10:]:  # Last 10 unseen max
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            sender = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))
            body = _extract_text(msg)

            results.append({
                "msg_id": msg_id,
                "from": sender,
                "subject": subject,
                "body": body,
            })

            # Mark as seen
            mail.store(msg_id, "+FLAGS", "\\Seen")

        mail.logout()
    except Exception as e:
        logger.error(f"IMAP error for {address}: {e}")

    return results

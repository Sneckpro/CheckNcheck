import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# Known receipt senders (lowercase substrings)
RECEIPT_SENDERS = [
    "wolt", "bolt", "glovo", "uber", "yandex", "delivery",
    "amazon", "ebay", "aliexpress", "paypal", "stripe",
    "apple", "google play", "netflix", "spotify", "youtube",
    "booking", "airbnb", "ryanair", "wizz",
    "lidl", "aldi", "ikea", "zara", "h&m",
    "noreply", "no-reply", "receipt", "invoice", "payment",
    "order", "confirmation",
]

# Keywords in subject (lowercase)
RECEIPT_KEYWORDS = [
    "receipt", "invoice", "order", "payment", "confirmation",
    "чек", "заказ", "оплата", "подтверждение", "квитанция",
    "purchase", "transaction", "billing", "subscription",
    "доставка", "delivery", "shipped",
]


def _looks_like_receipt(sender: str, subject: str) -> bool:
    """Quick filter: does this email look like a receipt/payment?"""
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    for s in RECEIPT_SENDERS:
        if s in sender_lower:
            return True
    for kw in RECEIPT_KEYWORDS:
        if kw in subject_lower:
            return True
    return False


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
    return "\n".join(texts)[:3000]


def fetch_emails(server: str, address: str, password: str,
                 since_days: int | None = None) -> list[dict]:
    """Fetch emails from IMAP. Returns list with stable UID for dedup."""
    results = []
    try:
        mail = imaplib.IMAP4_SSL(server)
        mail.login(address, password)
        mail.select("INBOX", readonly=True)

        if since_days:
            since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            _, message_nums = mail.search(None, f'(SINCE "{since_date}")')
        else:
            # Default: last 2 days
            since_date = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
            _, message_nums = mail.search(None, f'(SINCE "{since_date}")')

        if not message_nums[0]:
            mail.logout()
            return results

        for msg_num in message_nums[0].split()[-50:]:  # Max 50 per scan
            _, msg_data = mail.fetch(msg_num, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            # Use Message-ID as stable unique identifier
            message_id = msg.get("Message-ID", "")
            if not message_id:
                message_id = f"{msg.get('Date', '')}_{msg.get('From', '')}_{msg.get('Subject', '')}"

            sender = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))

            if not _looks_like_receipt(sender, subject):
                continue

            body = _extract_text(msg)

            results.append({
                "uid": message_id,
                "from": sender,
                "subject": subject,
                "body": body,
            })

        mail.logout()
    except Exception as e:
        logger.error(f"IMAP error for {address}: {e}")

    return results

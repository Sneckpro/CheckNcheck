import imaplib
import email
import re
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
    "ozon", "wildberries", "webmoney", "tinkoff", "sber",
    "receipt", "invoice",
]

# Keywords in subject (lowercase)
RECEIPT_KEYWORDS = [
    "receipt", "invoice", "order", "payment", "confirmation",
    "чек", "заказ", "оплат", "подтвержден", "квитанц",
    "purchase", "transaction", "billing", "subscription",
    "доставк", "delivery", "shipped",
    # Serbian
    "porudž", "porudz", "račun", "racun", "isporuč", "isporuc",
    "narudž", "narudz", "uplat", "potvrda",
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


def _strip_html(html: str) -> str:
    """Convert HTML to plain text."""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li|h\d)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()


def _extract_text(msg: email.message.Message) -> str:
    """Extract text content from email message."""
    plain_texts = []
    html_texts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                plain_texts.append(decoded)
            elif content_type == "text/html":
                html_texts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_texts.append(decoded)
            else:
                plain_texts.append(decoded)

    # Prefer plain text, fall back to stripped HTML
    if plain_texts:
        return "\n".join(plain_texts)[:3000]
    if html_texts:
        return _strip_html("\n".join(html_texts))[:3000]
    return ""


def fetch_emails(server: str, address: str, password: str,
                 since_days: int | None = None, debug: bool = False) -> dict:
    """Fetch emails from IMAP. Returns dict with results, stats, and debug info."""
    results = []
    skipped = []
    total_count = 0
    try:
        mail = imaplib.IMAP4_SSL(server)
        mail.login(address, password)
        mail.select("INBOX", readonly=True)

        if since_days:
            since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            _, message_nums = mail.uid('search', None, f'(SINCE "{since_date}")')
        else:
            since_date = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
            _, message_nums = mail.uid('search', None, f'(SINCE "{since_date}")')

        if not message_nums[0]:
            mail.logout()
            return {"results": [], "skipped": [], "total": 0}

        msg_uids = message_nums[0].split()[-50:]
        total_count = len(msg_uids)

        for msg_uid in msg_uids:
            _, msg_data = mail.uid('fetch', msg_uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            message_id = msg.get("Message-ID", "")
            if not message_id:
                message_id = f"{msg.get('Date', '')}_{msg.get('From', '')}_{msg.get('Subject', '')}"

            sender = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))

            if not _looks_like_receipt(sender, subject):
                logger.debug("SKIP receipt filter: from=%s subj=%s", sender[:50], subject[:60])
                if debug:
                    skipped.append({"from": sender[:50], "subject": subject[:60]})
                continue

            body = _extract_text(msg)
            logger.info("PASS receipt filter: from=%s subj=%s body_len=%d",
                        sender[:50], subject[:60], len(body))

            results.append({
                "uid": message_id,
                "from": sender,
                "subject": subject,
                "body": body,
            })

        logger.info("IMAP fetch done: total=%d, passed_filter=%d, skipped=%d",
                    total_count, len(results), total_count - len(results))
        mail.logout()
    except Exception as e:
        logger.error(f"IMAP error for {address}: {e}")

    return {"results": results, "skipped": skipped, "total": total_count}

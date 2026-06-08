import base64
import imaplib
import re
from datetime import datetime
from email import message_from_bytes
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Union


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _imap_date(value: datetime) -> str:
    return value.strftime("%d-%b-%Y")


def _parse_internal_date(raw: Union[bytes, str], fallback: str = "") -> int:
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    match = re.search(r'INTERNALDATE "([^"]+)"', text)
    date_value = match.group(1) if match else fallback
    try:
        return int(parsedate_to_datetime(date_value).timestamp() * 1000)
    except Exception:
        return 0


def _header_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    text = part.get_payload()
    if isinstance(text, str):
        return text.encode(part.get_content_charset() or "utf-8", errors="ignore")
    return b""


def _payload_for_message(msg: Message) -> dict:
    headers = [{"name": key, "value": value} for key, value in msg.items()]

    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part is msg:
                continue
            if part.get_content_maintype() == "multipart":
                continue
            parts.append({
                "mimeType": part.get_content_type(),
                "body": {"data": _b64url(_header_bytes(part))},
            })
        return {
            "mimeType": msg.get_content_type(),
            "headers": headers,
            "parts": parts,
        }

    return {
        "mimeType": msg.get_content_type(),
        "headers": headers,
        "body": {"data": _b64url(_header_bytes(msg))},
    }


class ImapGmailClient:
    """Tiny Gmail-API-shaped wrapper over Gmail IMAP for backfill scripts."""

    def __init__(self, email: str, password: str, host: str = "imap.gmail.com"):
        self.email = email
        self.password = password
        self.host = host
        self._messages: dict[str, dict] = {}
        self._threads: dict[str, list[dict]] = {}
        self._loaded: set[tuple[str, str, str]] = set()
        self._last_range: tuple[datetime, datetime] | None = None
        self._mailboxes: dict[str, str] | None = None

    def users(self):
        return _UsersResource(self)

    def _connect(self):
        imap = imaplib.IMAP4_SSL(self.host)
        imap.login(self.email, self.password)
        return imap

    def _discover_mailboxes(self, imap) -> dict[str, str]:
        if self._mailboxes:
            return self._mailboxes

        mailboxes = {"inbox": "INBOX", "sent": "[Gmail]/Sent Mail"}
        status, data = imap.list()
        if status == "OK":
            names = []
            for item in data:
                text = item.decode("utf-8", errors="ignore")
                match = re.search(r' "([^"]+)"$', text)
                if match:
                    names.append(match.group(1))
            for name in names:
                lower = name.lower()
                if lower == "inbox":
                    mailboxes["inbox"] = name
                if "sent" in lower or "已发送" in lower:
                    mailboxes["sent"] = name

        self._mailboxes = mailboxes
        return mailboxes

    def _load_mailbox(self, kind: str, start: datetime, end: datetime):
        key = (kind, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if key in self._loaded:
            return

        imap = self._connect()
        try:
            mailbox = self._discover_mailboxes(imap)[kind]
            status, _ = imap.select(f'"{mailbox}"', readonly=True)
            if status != "OK":
                raise RuntimeError(f"cannot select mailbox {mailbox}")

            criteria = ["SINCE", _imap_date(start), "BEFORE", _imap_date(end)]
            status, data = imap.uid("search", None, *criteria)
            if status != "OK":
                raise RuntimeError(f"imap search failed for {mailbox}: {status}")

            for uid in data[0].split():
                status, fetched = imap.uid("fetch", uid, "(X-GM-THRID INTERNALDATE BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = b""
                meta = b""
                for item in fetched:
                    if isinstance(item, tuple):
                        meta += item[0] or b""
                        raw += item[1] or b""
                if not raw:
                    continue
                thread_match = re.search(rb"X-GM-THRID (\d+)", meta)
                thread_id = thread_match.group(1).decode("ascii") if thread_match else uid.decode("ascii")
                msg = message_from_bytes(raw)
                internal_date = _parse_internal_date(meta, msg.get("Date", ""))
                msg_id = f"{self.email}:{kind}:{uid.decode('ascii')}"
                api_msg = {
                    "id": msg_id,
                    "threadId": thread_id,
                    "labelIds": ["SENT"] if kind == "sent" else ["INBOX"],
                    "internalDate": str(internal_date),
                    "payload": _payload_for_message(msg),
                }
                self._messages[msg_id] = api_msg
                self._threads.setdefault(thread_id, [])
                if not any(existing["id"] == msg_id for existing in self._threads[thread_id]):
                    self._threads[thread_id].append(api_msg)
        finally:
            try:
                imap.close()
            except Exception:
                pass
            imap.logout()

        self._loaded.add(key)

    def _load_range(self, kind: str, query: str):
        after_match = re.search(r"after:(\d{4}/\d{1,2}/\d{1,2})", query)
        before_match = re.search(r"before:(\d{4}/\d{1,2}/\d{1,2})", query)
        if not after_match or not before_match:
            raise RuntimeError(f"IMAP mode requires after:/before: query dates: {query}")
        start = datetime.strptime(after_match.group(1), "%Y/%m/%d")
        end = datetime.strptime(before_match.group(1), "%Y/%m/%d")
        self._last_range = (start, end)
        self._load_mailbox(kind, start, end)

    def _load_all_for_last_range(self):
        if not self._last_range:
            return
        start, end = self._last_range
        self._load_mailbox("inbox", start, end)
        self._load_mailbox("sent", start, end)


class _UsersResource:
    def __init__(self, client: ImapGmailClient):
        self._client = client

    def messages(self):
        return _MessagesResource(self._client)

    def threads(self):
        return _ThreadsResource(self._client)

    def getProfile(self, userId="me"):
        return _Request(lambda: {"emailAddress": self._client.email})


class _MessagesResource:
    def __init__(self, client: ImapGmailClient):
        self._client = client

    def list(self, userId="me", q="", maxResults=100, pageToken=None):
        def run():
            kind = "sent" if "in:sent" in q else "inbox"
            self._client._load_range(kind, q)
            refs = [
                {"id": msg["id"], "threadId": msg["threadId"]}
                for msg in self._client._messages.values()
                if ("SENT" in msg.get("labelIds", [])) == (kind == "sent")
            ]
            refs.sort(key=lambda ref: int(self._client._messages[ref["id"]].get("internalDate", "0")))
            return {"messages": refs[-maxResults:]}

        return _Request(run)

    def get(self, userId="me", id="", format="full"):
        return _Request(lambda: self._client._messages[id])


class _ThreadsResource:
    def __init__(self, client: ImapGmailClient):
        self._client = client

    def get(self, userId="me", id="", format="full"):
        def run():
            self._client._load_all_for_last_range()
            messages = sorted(
                self._client._threads.get(id, []),
                key=lambda msg: int(msg.get("internalDate", "0")),
            )
            return {"messages": messages}

        return _Request(run)

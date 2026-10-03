"""Parse WhatsApp export records without interpreting their content."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter

STAMP = r"(\d{1,4}[/.\-]\d{1,2}[/.\-]\d{2,4}),?\s+(\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s*[AaPp][Mm])?)"
IOS = re.compile(r"^\[" + STAMP + r"\]\s*(.*)$")
ANDROID = re.compile(r"^" + STAMP + r"\s+-\s+(.*)$")
INVISIBLE = "\u200e\u200f\u202a\u202b\u202c\u2066\u2067\u2068\u2069\ufeff"


class NoChatMessages(ValueError):
    """Text contains no dated WhatsApp records."""


def timestamp(date: str, clock: str, order: str) -> str:
    parts = re.split(r"[/.\-]", date)
    if len(parts[0]) == 4:
        year, month, day = map(int, parts)
    else:
        first, second, year = map(int, parts)
        day, month = (first, second) if order == "day-first" else (second, first)
        if year < 100:
            year += 2000
    clock = clock.replace(".", ":").strip().upper()
    for pattern in (
        "%H:%M:%S",
        "%H:%M",
        "%I:%M:%S %p",
        "%I:%M %p",
        "%I:%M:%S%p",
        "%I:%M%p",
    ):
        try:
            time = dt.datetime.strptime(clock, pattern).time()  # noqa: DTZ007 - Exports do not establish a timezone.
            return dt.datetime.combine(dt.date(year, month, day), time).isoformat()
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse WhatsApp timestamp {date!r} {clock!r} using {order}"
    )


def parse_chat(text: str, order: str = "day-first") -> list[dict]:
    messages = []
    preamble = []
    for raw in text.splitlines():
        # Directional marks wrap timestamps in iOS exports; retain marks inside message bodies.
        line = raw.lstrip(INVISIBLE).replace("\u202f", " ").replace("\xa0", " ")
        match = IOS.match(line) or ANDROID.match(line)
        if match:
            date, clock, body = match.groups()
            sender, separator, content = body.partition(": ")
            messages.append(
                {
                    "timestamp": timestamp(date, clock, order),
                    "sender": sender if separator else None,
                    "body": content if separator else body,
                }
            )
        elif messages:
            messages[-1]["body"] += "\n" + raw
        elif raw.strip(INVISIBLE + " \t"):
            preamble.append(raw)
    if not messages:
        raise NoChatMessages("No dated WhatsApp messages found in the chat text")
    if preamble:
        messages.insert(
            0, {"timestamp": None, "sender": None, "body": "\n".join(preamble)}
        )
    return messages


def message_ids(messages: list[dict], conversation: str):
    """Use maximum occurrence counts across exports, retaining repeated identical messages."""
    occurrences = Counter()
    for message in messages:
        payload = json.dumps(
            [conversation, message["timestamp"], message["sender"], message["body"]],
            ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        occurrences[fingerprint] += 1
        yield f"{fingerprint}-{occurrences[fingerprint]}", message


def attachment_names(body: str) -> list[str]:
    names = re.findall(
        r"<(?:attached|terlampir):\s*([^>]+)>", body, flags=re.IGNORECASE
    )
    names += re.findall(
        r"^(.+?)\s+\((?:file attached|file terlampir|berkas terlampir)\)",
        body,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return [name.strip(INVISIBLE + " ") for name in names]

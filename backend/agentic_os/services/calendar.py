"""A small iCalendar (RFC 5545) reader/writer for ~/Calendar/calendar.ics.

Only what the agent needs: VEVENT with UID, SUMMARY, DTSTART, DTEND, LOCATION, DESCRIPTION, URL,
ORGANIZER and ATTENDEE. Times are local "floating" times (no time zone), which every calendar app
imports as the viewer's local time.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional

PRODID = "-//Agentic OS//Calendar 1.0//EN"


def _esc(text: str) -> str:
    return (str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n"))


def _unesc(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    parts.append(cur)
    return "\r\n ".join(parts)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def parse_dt(value: str) -> datetime:
    v = value.strip().rstrip("Z")
    for f in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(v, f)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


def parse_user_time(value: str) -> datetime:
    """Accept ISO strings ('2026-11-20T09:30', '2026-11-20 09:30')."""
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", ""))
    except ValueError:
        raise ValueError("use a date and time like 2026-11-20T09:30") from None
    return dt.replace(tzinfo=None, second=0, microsecond=0)


def parse_ics(text: str) -> List[Dict]:
    lines: List[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {"attendees": []}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("uid") and cur.get("start"):
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            name = key.split(";", 1)[0].upper()
            if name == "UID":
                cur["uid"] = val
            elif name == "SUMMARY":
                cur["title"] = _unesc(val)
            elif name == "DTSTART":
                cur["start"] = parse_dt(val).isoformat(timespec="minutes")
            elif name == "DTEND":
                cur["end"] = parse_dt(val).isoformat(timespec="minutes")
            elif name == "LOCATION":
                cur["location"] = _unesc(val)
            elif name == "DESCRIPTION":
                cur["description"] = _unesc(val)
            elif name == "URL":
                cur["url"] = val
            elif name == "ATTENDEE":
                cur["attendees"].append(val.split(":", 1)[-1] if val.lower().startswith("mailto:") else val)
            elif name == "ORGANIZER":
                cur["organizer"] = val.split(":", 1)[-1]
    events.sort(key=lambda e: e["start"])
    return events


def event_lines(ev: Dict, *, stamp: Optional[datetime] = None) -> List[str]:
    out = ["BEGIN:VEVENT", f"UID:{ev['uid']}", f"DTSTAMP:{fmt_dt(stamp or datetime.now())}",
           f"DTSTART:{fmt_dt(parse_user_time(ev['start']))}", f"DTEND:{fmt_dt(parse_user_time(ev['end']))}",
           f"SUMMARY:{_esc(ev.get('title') or 'Event')}"]
    if ev.get("location"):
        out.append(f"LOCATION:{_esc(ev['location'])}")
    if ev.get("description"):
        out.append(f"DESCRIPTION:{_esc(ev['description'])}")
    if ev.get("url"):
        out.append(f"URL:{ev['url']}")
    if ev.get("organizer"):
        out.append(f"ORGANIZER:mailto:{ev['organizer']}")
    for a in ev.get("attendees") or []:
        out.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{a}")
    out.append("END:VEVENT")
    return out


def render_ics(events: List[Dict], *, method: Optional[str] = None) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    if method:
        lines.append(f"METHOD:{method}")
    for ev in sorted(events, key=lambda e: e["start"]):
        lines.extend(event_lines(ev))
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def new_uid(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:20] + "@agentic-os.local"


def jitsi_link(title: str) -> str:
    slug = "".join(ch for ch in title.title() if ch.isalnum())[:24] or "Meeting"
    return f"https://meet.jit.si/AgenticOS-{slug}-{secrets.token_hex(5)}"


def overlaps(events: List[Dict], start: datetime, end: datetime) -> List[Dict]:
    out = []
    for e in events:
        s, en = parse_user_time(e["start"]), parse_user_time(e.get("end") or e["start"])
        if s < end and en > start:
            out.append(e)
    return out


def in_range(events: List[Dict], start: datetime, days: int) -> List[Dict]:
    stop = start + timedelta(days=days)
    return [e for e in events if start <= parse_user_time(e["start"]) < stop
            or (parse_user_time(e["start"]) < start < parse_user_time(e.get("end") or e["start"]))]

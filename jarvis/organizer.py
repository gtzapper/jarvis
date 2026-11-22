"""Reminders, notes, a calendar of events and bills, the daily report, and a private calendar feed for your phone.

Everything is stored in data/organizer.db (SQLite) on the Jarvis host. Times are local (the container's TZ)."""

import calendar as cal
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

DB = Path("data") / "organizer.db"
REPEATS = ["", "daily", "weekdays", "weekly", "monthly", "yearly"]
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, due TEXT NOT NULL, repeat TEXT DEFAULT '',
    done INTEGER DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT DEFAULT 'event', day TEXT NOT NULL,
    time TEXT DEFAULT '', amount REAL, repeat TEXT DEFAULT '', remind_days INTEGER DEFAULT 3, paid INTEGER DEFAULT 0,
    notes TEXT DEFAULT '', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT, title TEXT, text TEXT);
"""


@contextmanager
def db():
    DB.parent.mkdir(exist_ok=True)
    with _lock:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(SCHEMA)
            yield con
            con.commit()
        finally:
            con.close()


def now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------- repeats

def _add_months(d: date, n: int, day: int | None = None) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(day or d.day, cal.monthrange(y, m)[1]))


def next_after(d: date, repeat: str, anchor_day: int | None = None) -> date:
    """The next occurrence after d."""
    if repeat == "daily":
        return d + timedelta(days=1)
    if repeat == "weekdays":
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    if repeat == "weekly":
        return d + timedelta(days=7)
    if repeat == "monthly":
        return _add_months(d, 1, anchor_day)
    if repeat == "yearly":
        return _add_months(d, 12, anchor_day)
    raise ValueError(f"not repeating: {repeat!r}")


def occurrences(start: date, repeat: str, frm: date, to: date) -> list[date]:
    """Dates of an item between frm and to (inclusive), following its repeat."""
    out, d, anchor = [], start, start.day
    if not repeat:
        return [d] if frm <= d <= to else []
    guard = 0
    while d <= to and guard < 2000:
        if d >= frm:
            out.append(d)
        d = next_after(d, repeat, anchor)
        guard += 1
    return out


# ---------------------------------------------------------------- understanding times (when the AI isn't on)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m.lower(): i for i, m in enumerate(cal.month_name) if m} | {m.lower(): i for i, m in enumerate(cal.month_abbr) if m}
TIME_RE = r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?"


def _time(m) -> tuple[int, int] | None:
    if not m or m.group("h") is None:
        return None
    h, mi, ap = int(m.group("h")), int(m.group("m") or 0), (m.group("ap") or "").replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse_when(text: str, base: datetime | None = None) -> tuple[datetime | None, str, str]:
    """Find a time in text. Returns (when, repeat, text with the time words removed)."""
    base = base or now()
    t = " " + text.strip() + " "
    repeat = ""
    for pat, rep in [(r"\b(every ?day|daily)\b", "daily"), (r"\b(every weekday|on weekdays|weekdays)\b", "weekdays"),
                     (r"\b(every week|weekly)\b", "weekly"), (r"\b(every month|monthly)\b", "monthly"),
                     (r"\b(every year|yearly|annually)\b", "yearly")]:
        if re.search(pat, t, re.I):
            repeat = rep
            t = re.sub(pat, " ", t, flags=re.I)
    m = re.search(r"\bevery (" + "|".join(WEEKDAYS) + r")\b", t, re.I)
    if m:
        repeat = "weekly"
        t = t.replace(m.group(0), " on " + m.group(1))

    when = None
    m = re.search(r"\bin (an?|\d+(?:\.\d+)?) ?(minutes?|mins?|hours?|hrs?|days?|weeks?)\b", t, re.I)
    if m:
        n = 1.0 if m.group(1).lower() in ("a", "an") else float(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n) if unit.startswith(("hour", "hr")) else \
            timedelta(days=n) if unit.startswith("day") else timedelta(weeks=n)
        when = base + delta
        t = t.replace(m.group(0), " ")
    else:
        day = None
        if re.search(r"\btomorrow\b", t, re.I):
            day = base.date() + timedelta(days=1)
            t = re.sub(r"\btomorrow\b", " ", t, flags=re.I)
        elif re.search(r"\btonight\b", t, re.I):
            day = base.date()
            t = re.sub(r"\btonight\b", " at 8pm ", t, flags=re.I)
        elif re.search(r"\btoday\b", t, re.I):
            day = base.date()
            t = re.sub(r"\btoday\b", " ", t, flags=re.I)
        m = re.search(r"\b(?:on |next )?(" + "|".join(WEEKDAYS) + r")\b", t, re.I)
        if day is None and m:
            wd = WEEKDAYS.index(m.group(1).lower())
            ahead = (wd - base.weekday()) % 7 or 7
            day = base.date() + timedelta(days=ahead)
            t = t.replace(m.group(0), " ")
        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
        if day is None and m:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            t = t.replace(m.group(0), " ")
        mon = "|".join(sorted(MONTHS, key=len, reverse=True))
        m = re.search(rf"\b(?:on )?(?:the )?(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? ({mon})\b|\b(?:on )?({mon}) (\d{{1,2}})(?:st|nd|rd|th)?\b", t, re.I)
        if day is None and m:
            dd, mm = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            mm = MONTHS[mm.lower()]
            day = date(base.year, mm, min(int(dd), cal.monthrange(base.year, mm)[1]))
            if day < base.date():
                day = date(base.year + 1, mm, min(int(dd), cal.monthrange(base.year + 1, mm)[1]))
            t = t.replace(m.group(0), " ")
        m = re.search(r"\b(?:on )?the (\d{1,2})(?:st|nd|rd|th)\b", t, re.I)
        if day is None and m:
            dd = int(m.group(1))
            day = date(base.year, base.month, min(dd, cal.monthrange(base.year, base.month)[1]))
            if day < base.date():
                day = _add_months(day, 1, dd)
            t = t.replace(m.group(0), " ")
        m = re.search(r"\bat " + TIME_RE + r"\b|\b" + r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<ap2>am|pm)\b", t, re.I)
        hm = None
        if m:
            if m.group("h") is not None:
                hm = _time(m)
            else:
                hm = _time(re.match(TIME_RE, f"{m.group('h2')}:{m.group('m2') or '00'}{m.group('ap2')}", re.I))
            t = t.replace(m.group(0), " ")
        if re.search(r"\b(morning)\b", t, re.I) and not hm:
            hm = (9, 0)
            t = re.sub(r"\b(in the |this )?morning\b", " ", t, flags=re.I)
        if re.search(r"\b(evening)\b", t, re.I) and not hm:
            hm = (18, 0)
            t = re.sub(r"\b(in the |this )?evening\b", " ", t, flags=re.I)
        if day is not None or hm is not None:
            day = day or base.date()
            h, mi = hm or (9, 0)
            when = datetime(day.year, day.month, day.day, h, mi)
            if when <= base and hm and day == base.date():
                when += timedelta(days=1)  # "at 7am" when it's already 10am means tomorrow
    rest = re.sub(r"\s+", " ", re.sub(r"\b(on|at)\s*$", "", t.strip(), flags=re.I)).strip(" ,.")
    return when, repeat, rest


# ---------------------------------------------------------------- notes

STOPWORDS = set("the a an is are was were what which who whom does do did my me i you it its in on at of for to and or "
                "with about that this there have has had use uses used any some our your".split())

def add_note(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("The note is empty")
    with db() as c:
        return c.execute("INSERT INTO notes (text, created) VALUES (?, ?)", (text, _iso(now()))).lastrowid


def notes(query: str = "", limit: int = 50) -> list[dict]:
    with db() as c:
        if query:
            words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1 and w not in STOPWORDS]
            rows = c.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
            scored = [(sum(w in r["text"].lower() for w in words), r) for r in rows]
            return [dict(r) for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0][:limit]
        return [dict(r) for r in c.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))]


def delete_note(note_id: int) -> bool:
    with db() as c:
        return c.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0


# ---------------------------------------------------------------- reminders

def add_reminder(text: str, due: datetime, repeat: str = "") -> int:
    if repeat not in REPEATS:
        raise ValueError(f"Repeat must be one of: {', '.join(r or 'none' for r in REPEATS)}")
    text = text.strip() or "Reminder"
    with db() as c:
        return c.execute("INSERT INTO reminders (text, due, repeat, created) VALUES (?, ?, ?, ?)",
                         (text, _iso(due), repeat, _iso(now()))).lastrowid


def reminders(include_done: bool = False) -> list[dict]:
    with db() as c:
        q = "SELECT * FROM reminders" + ("" if include_done else " WHERE done = 0") + " ORDER BY due"
        return [dict(r) for r in c.execute(q)]


def cancel_reminder(rid: int) -> bool:
    with db() as c:
        return c.execute("DELETE FROM reminders WHERE id = ?", (rid,)).rowcount > 0


# ---------------------------------------------------------------- events and bills

def add_event(title: str, day: date, kind: str = "event", time: str = "", amount: float | None = None,
              repeat: str = "", remind_days: int | None = None, notes_: str = "") -> int:
    if kind not in ("event", "bill"):
        raise ValueError("kind must be event or bill")
    if repeat not in REPEATS:
        raise ValueError(f"Repeat must be one of: {', '.join(r or 'none' for r in REPEATS)}")
    if time and not re.fullmatch(r"\d{2}:\d{2}", time):
        raise ValueError("time must be HH:MM")
    remind_days = (3 if kind == "bill" else 0) if remind_days is None else max(0, int(remind_days))
    with db() as c:
        return c.execute("INSERT INTO events (title, kind, day, time, amount, repeat, remind_days, notes, created) VALUES (?,?,?,?,?,?,?,?,?)",
                         (title.strip(), kind, day.isoformat(), time, amount, repeat, remind_days, notes_, _iso(now()))).lastrowid


def events() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY day")]


def delete_event(eid: int) -> bool:
    with db() as c:
        return c.execute("DELETE FROM events WHERE id = ?", (eid,)).rowcount > 0


def find_event(name_or_id) -> dict | None:
    rows = events()
    if str(name_or_id).isdigit():
        return next((e for e in rows if e["id"] == int(name_or_id)), None)
    q = str(name_or_id).lower().strip()
    return next((e for e in rows if e["title"].lower() == q), None) or next((e for e in rows if q in e["title"].lower()), None)


def mark_paid(name_or_id) -> str:
    e = find_event(name_or_id)
    if not e or e["kind"] != "bill":
        raise ValueError(f"I don't have a bill called '{name_or_id}'")
    with db() as c:
        if e["repeat"]:
            d = date.fromisoformat(e["day"])
            nxt = next_after(d, e["repeat"], d.day)
            # a bill paid early still moves to the cycle after its current due date
            c.execute("UPDATE events SET day = ? WHERE id = ?", (nxt.isoformat(), e["id"]))
            return f"Marked {e['title']} paid. Next one is due {nxt:%a %d %b}."
        c.execute("UPDATE events SET paid = 1 WHERE id = ?", (e["id"],))
        return f"Marked {e['title']} paid."


def upcoming(days: int = 14, start: date | None = None) -> list[dict]:
    """Every event, bill and reminder from start (today) for the next N days, repeats expanded, in date order."""
    frm = start or now().date()
    to = frm + timedelta(days=days)
    out = []
    for e in events():
        if e["kind"] == "bill" and e["paid"]:
            continue
        for d in occurrences(date.fromisoformat(e["day"]), e["repeat"], frm if e["kind"] != "bill" else date.min, to):
            overdue = e["kind"] == "bill" and d < frm
            if d < frm and not overdue:
                continue
            out.append({"kind": e["kind"], "id": e["id"], "title": e["title"], "date": d.isoformat(), "time": e["time"],
                        "amount": e["amount"], "repeat": e["repeat"], "overdue": overdue})
            if overdue:
                break  # only the first unpaid cycle counts as overdue
    for r in reminders():
        due = datetime.fromisoformat(r["due"])
        for d in occurrences(due.date(), r["repeat"], frm, to):
            out.append({"kind": "reminder", "id": r["id"], "title": r["text"], "date": d.isoformat(), "time": due.strftime("%H:%M"),
                        "amount": None, "repeat": r["repeat"], "overdue": False})
    return sorted(out, key=lambda x: (x["date"], x["time"] or "00:00"))


def describe(item: dict) -> str:
    d = date.fromisoformat(item["date"])
    today = now().date()
    when = "today" if d == today else "tomorrow" if d == today + timedelta(days=1) else f"{d:%a %d %b}"
    if item["overdue"]:
        when = f"overdue since {d:%d %b}"
    tm = f" at {item['time']}" if item["time"] else ""
    amt = f" ({money(item['amount'])})" if item["amount"] else ""
    tag = {"bill": "Bill: ", "reminder": "Reminder: ", "event": ""}[item["kind"]]
    rep = f", {item['repeat']}" if item["repeat"] else ""
    return f"{tag}{item['title']}{amt}, {when}{tm}{rep}"


def money(x: float) -> str:
    if x is None:
        return ""
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


# ---------------------------------------------------------------- what's due now (the scheduler)

def _once(c, key: str) -> bool:
    """True the first time a key is seen, so nothing is announced twice."""
    try:
        c.execute("INSERT INTO sent (key, at) VALUES (?, ?)", (key, _iso(now())))
        return True
    except sqlite3.IntegrityError:
        return False


def due_now(at: datetime | None = None) -> list[tuple[str, str, str]]:
    """Reminders that are due, bills coming up or due today, and timed events starting soon.
    Returns (kind, title, text) to deliver, and moves repeating reminders on."""
    at = at or now()
    out = []
    with db() as c:
        for r in c.execute("SELECT * FROM reminders WHERE done = 0 AND due <= ?", (_iso(at),)).fetchall():
            out.append(("reminder", "Reminder", r["text"]))
            if r["repeat"]:
                due = datetime.fromisoformat(r["due"])
                nxt = due
                while nxt <= at:
                    d = next_after(nxt.date(), r["repeat"], due.day)
                    nxt = datetime(d.year, d.month, d.day, due.hour, due.minute)
                c.execute("UPDATE reminders SET due = ? WHERE id = ?", (_iso(nxt), r["id"]))
            else:
                c.execute("UPDATE reminders SET done = 1 WHERE id = ?", (r["id"],))
        for e in c.execute("SELECT * FROM events WHERE paid = 0").fetchall():
            start = date.fromisoformat(e["day"])
            if e["kind"] == "bill":
                if at.hour < 9:   # bill notices go out from 9am, not in the middle of the night
                    continue
                for d in occurrences(start, e["repeat"], at.date(), at.date() + timedelta(days=e["remind_days"] or 0)):
                    left = (d - at.date()).days
                    amt = f" ({money(e['amount'])})" if e["amount"] else ""
                    if left == 0 and _once(c, f"bill:{e['id']}:{d}:due"):
                        out.append(("bill", "Bill due today", f"{e['title']}{amt} is due today."))
                    elif left == e["remind_days"] and left > 0 and _once(c, f"bill:{e['id']}:{d}:ahead"):
                        out.append(("bill", "Bill coming up", f"{e['title']}{amt} is due in {left} day{'s' if left > 1 else ''}, on {d:%a %d %b}."))
            elif e["time"]:
                for d in occurrences(start, e["repeat"], at.date(), at.date()):
                    h, m = map(int, e["time"].split(":"))
                    starts = datetime(d.year, d.month, d.day, h, m)
                    if timedelta(0) <= starts - at <= timedelta(minutes=30) and _once(c, f"event:{e['id']}:{d}"):
                        out.append(("event", "Coming up", f"{e['title']} at {e['time']}."))
        for kind, title, text in out:
            c.execute("INSERT INTO outbox (at, kind, title, text) VALUES (?, ?, ?, ?)", (_iso(at), kind, title, text))
    return out


def report_due(report_time: str, at: datetime | None = None) -> bool:
    """True once a day, at (or soon after) the configured report time."""
    if not report_time or not re.fullmatch(r"\d{1,2}:\d{2}", report_time):
        return False
    at = at or now()
    h, m = map(int, report_time.split(":"))
    if (at.hour, at.minute) < (h, m) or at - at.replace(hour=h, minute=m) > timedelta(hours=3):
        return False
    with db() as c:
        return _once(c, f"report:{at.date()}")


def record(kind: str, title: str, text: str) -> int:
    """Put something in the outbox (the app and listening devices pick it up from there)."""
    with db() as c:
        return c.execute("INSERT INTO outbox (at, kind, title, text) VALUES (?, ?, ?, ?)", (_iso(now()), kind, title, text)).lastrowid


def outbox(after: int = 0, limit: int = 50) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM outbox WHERE id > ? ORDER BY id LIMIT ?", (after, limit))]


def last_outbox_id() -> int:
    with db() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM outbox").fetchone()[0]


# ---------------------------------------------------------------- the daily report

def agenda_text(days: int = 7) -> list[str]:
    items = upcoming(days)
    today = now().date().isoformat()
    lines = []
    todays = [i for i in items if i["date"] == today and i["kind"] != "bill"]
    bills = [i for i in items if i["kind"] == "bill"]
    if todays:
        lines.append("Today: " + "; ".join(describe(i).replace(", today", "") for i in todays) + ".")
    else:
        lines.append("Nothing on the calendar today.")
    if bills:
        lines.append("Bills: " + "; ".join(describe(i).replace("Bill: ", "") for i in bills) + ".")
    else:
        lines.append("No bills due this week.")
    return lines


def daily_report(lab_summary: str | None = None, alerts: list[str] | None = None, extra: list[str] | None = None) -> str:
    hour = now().hour
    greet = "Good morning." if hour < 12 else "Good afternoon." if hour < 18 else "Good evening."
    parts = [f"{greet} It's {now():%A %d %B}."]
    if lab_summary:
        parts.append(lab_summary)
    if alerts:
        parts.append(f"Since yesterday: {len(alerts)} alert{'s' if len(alerts) > 1 else ''}, the latest: {alerts[0]}.")
    parts += agenda_text()
    parts += extra or []
    return "\n".join(parts)


# ---------------------------------------------------------------- calendar feed for your phone

def feed_token() -> str:
    p = Path("data") / "calendar_token"
    if not p.exists():
        p.parent.mkdir(exist_ok=True)
        p.write_text(secrets.token_urlsafe(24))
    return p.read_text().strip()


def feed_enabled() -> bool:
    """The calendar link only works after you choose to publish it (Planner > Publish to phone calendar)."""
    return (Path("data") / "calendar_published").exists()


def set_feed_enabled(on: bool) -> None:
    p = Path("data") / "calendar_published"
    p.parent.mkdir(exist_ok=True)
    if on:
        p.write_text("1")
    else:
        p.unlink(missing_ok=True)


def reset_feed_token() -> str:
    (Path("data") / "calendar_token").unlink(missing_ok=True)
    return feed_token()


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


RRULE = {"daily": "FREQ=DAILY", "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "weekly": "FREQ=WEEKLY",
         "monthly": "FREQ=MONTHLY", "yearly": "FREQ=YEARLY"}


def ics() -> str:
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Jarvis//Homelab//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Jarvis", "METHOD:PUBLISH"]

    def event(uid, title, d: date, time_: str, repeat: str, desc: str = "", alarm_days: int = 0):
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@jarvis", f"DTSTAMP:{stamp}", f"SUMMARY:{_ics_escape(title)}"])
        if time_:
            h, m = map(int, time_.split(":"))
            st = datetime(d.year, d.month, d.day, h, m)
            lines.extend([f"DTSTART:{st:%Y%m%dT%H%M%S}", f"DTEND:{st + timedelta(minutes=30):%Y%m%dT%H%M%S}"])
        else:
            lines.extend([f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"])
        if repeat in RRULE:
            lines.append("RRULE:" + RRULE[repeat])
        if desc:
            lines.append("DESCRIPTION:" + _ics_escape(desc))
        if alarm_days:
            lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(title)}", f"TRIGGER:-P{alarm_days}D", "END:VALARM"])
        lines.append("END:VEVENT")

    for e in events():
        if e["kind"] == "bill" and e["paid"]:
            continue
        title = (f"Bill: {e['title']}" + (f" ({money(e['amount'])})" if e["amount"] else "")) if e["kind"] == "bill" else e["title"]
        event(f"event-{e['id']}-{e['day']}", title, date.fromisoformat(e["day"]), e["time"], e["repeat"], e["notes"],
              e["remind_days"] if e["kind"] == "bill" else 0)
    for r in reminders():
        due = datetime.fromisoformat(r["due"])
        event(f"reminder-{r['id']}", f"Reminder: {r['text']}", due.date(), due.strftime("%H:%M"), r["repeat"])
    lines.append("END:VCALENDAR")
    folded = []
    for line in lines:  # RFC 5545: lines over 75 octets are folded
        while len(line.encode()) > 75:
            cut = 74
            while len(line[:cut].encode()) > 74:
                cut -= 1
            folded.append(line[:cut])
            line = " " + line[cut:]
        folded.append(line)
    return "\r\n".join(folded) + "\r\n"

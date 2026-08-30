import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass
class BookStats:
    title: str
    author: str | None
    total_seconds: int
    pages_read: int
    total_pages: int | None
    last_read_at: int | None
    months: dict[str, int] = field(default_factory=dict)
    days: dict[str, tuple[int, int]] = field(default_factory=dict)


def parse_koreader(data: bytes) -> list[BookStats]:
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("Not a KOReader statistics database (no SQLite header)")
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        con = sqlite3.connect(path)
        rows = con.execute(
            "SELECT id, title, authors, pages, total_read_time, total_read_pages, last_open FROM book"
        ).fetchall()
        monthly = con.execute(
            "SELECT id_book, strftime('%Y-%m', start_time, 'unixepoch') m, SUM(duration) FROM page_stat GROUP BY 1, 2"
        ).fetchall()
        daily = con.execute(
            "SELECT id_book, date(start_time, 'unixepoch', 'localtime') d, SUM(duration), COUNT(DISTINCT page) "
            "FROM page_stat_data GROUP BY 1, 2"
        ).fetchall()
    except sqlite3.DatabaseError:
        raise ValueError("Not a KOReader statistics database (unexpected schema)")
    finally:
        con.close()
        os.unlink(path)

    merged: dict[tuple[str, str], BookStats] = {}
    id_to_key: dict[int, tuple[str, str]] = {}
    for bid, title, authors, pages, secs, pread, last in rows:
        t = " ".join((title or "").split()) or "Untitled"
        a = " ".join(authors.split()).strip(";,") if authors and authors.strip() else None
        key = (norm(t), norm(a or ""))
        id_to_key[bid] = key
        if key not in merged:
            merged[key] = BookStats(t, a, 0, 0, None, None)
        m = merged[key]
        m.total_seconds += secs or 0
        m.pages_read += pread or 0
        m.total_pages = max(m.total_pages or 0, pages or 0) or None
        m.last_read_at = max(m.last_read_at or 0, last or 0) or None

    for bid, month, secs in monthly:
        key = id_to_key.get(bid)
        if key and month:
            months = merged[key].months
            months[month] = months.get(month, 0) + (secs or 0)

    for bid, day, secs, pgs in daily:
        key = id_to_key.get(bid)
        if key and day:
            days = merged[key].days
            prev = days.get(day, (0, 0))
            days[day] = (prev[0] + (secs or 0), prev[1] + (pgs or 0))
    return list(merged.values())

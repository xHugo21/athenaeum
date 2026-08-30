import re
import sqlite3
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
    md5: str | None = None
    sessions: list[tuple[int, int, int, int]] = field(default_factory=list)


def parse_koreader(data: bytes) -> list[BookStats]:
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("Not a KOReader statistics database (no SQLite header)")
    con = sqlite3.connect(":memory:")
    try:
        # ponytail: WAL-mode files can't deserialize into memory; flipping the header reads last checkpoint, no -wal merge
        if data[18] == 2:
            data = data[:18] + b"\x01\x01" + data[20:]
        con.deserialize(data)
        rows = con.execute(
            "SELECT id, title, authors, pages, total_read_time, total_read_pages, last_open, md5 FROM book"
        ).fetchall()
        sessions = con.execute(
            "SELECT id_book, page, start_time, duration, total_pages FROM page_stat_data"
        ).fetchall()
    except sqlite3.DatabaseError:
        raise ValueError("Not a KOReader statistics database (unexpected schema)")
    finally:
        con.close()

    merged: dict[tuple[str, str], BookStats] = {}
    id_to_key: dict[int, tuple[str, str]] = {}
    for bid, title, authors, pages, secs, pread, last, md5 in rows:
        t = " ".join((title or "").split()) or "Untitled"
        a = " ".join(authors.split()).strip(";,") if authors and authors.strip() else None
        key = (norm(t), norm(a or ""))
        id_to_key[bid] = key
        if key not in merged:
            merged[key] = BookStats(t, a, 0, 0, None, None)
        m = merged[key]
        m.md5 = m.md5 or md5
        m.total_seconds += secs or 0
        m.pages_read += pread or 0
        m.total_pages = max(m.total_pages or 0, pages or 0) or None
        m.last_read_at = max(m.last_read_at or 0, last or 0) or None

    for bid, page, start, dur, tp in sessions:
        key = id_to_key.get(bid)
        if key and start and dur:
            merged[key].sessions.append((page or 0, start, dur, tp or 0))
    return list(merged.values())

import os
import re
import sqlite3
import time
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .koreader import norm, parse_koreader

DB_PATH = os.environ.get("ATHENAEUM_DB", "athenaeum.db")
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


def ts_date(ts: int) -> str:
    return time.strftime("%-d %b %Y", time.localtime(ts))


templates.env.filters["ts_date"] = ts_date
templates.env.filters["day_fmt"] = lambda d: date.fromisoformat(d).strftime("%-d %b %Y")
templates.env.filters["read_hm"] = lambda s: f"{s // 3600}h {s % 3600 // 60:02d}min" if s >= 3600 else f"{s // 60}min"

app = FastAPI(title="athenaeum")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    isbn TEXT,
    md5 TEXT,
    total_seconds INTEGER NOT NULL DEFAULT 0,
    pages_read INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER,
    last_read_at INTEGER,
    rating REAL,
    review TEXT,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    start_time INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    total_pages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, page, start_time)
);
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY,
    seconds INTEGER NOT NULL,
    pages INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS book_days (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    pages INTEGER NOT NULL,
    PRIMARY KEY (book_id, day)
);
CREATE TABLE IF NOT EXISTS annotations (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    datetime TEXT NOT NULL,
    page_ref TEXT NOT NULL,
    type TEXT NOT NULL,
    text TEXT,
    note TEXT,
    chapter TEXT,
    pageno INTEGER,
    total_pages INTEGER,
    color TEXT,
    PRIMARY KEY (book_id, datetime, page_ref)
);
"""


@app.on_event("startup")
def init():
    with db() as con:
        con.executescript(SCHEMA)


def find_book(con, title: str, author: str | None, md5: str | None = None):
    if md5:
        r = con.execute("SELECT id FROM books WHERE md5=?", (md5,)).fetchone()
        if r:
            return r
    key = norm(title) + "|" + norm(author or "")
    # ponytail: O(n) scan; fine at personal-library scale, add a normalized column if it ever matters
    for r in con.execute("SELECT id, title, author, md5 FROM books"):
        if norm(r["title"]) + "|" + norm(r["author"] or "") == key:
            return r
    return None


@app.get("/")
def index(request: Request):
    sort = request.query_params.get("sort", "recent")
    d = request.query_params.get("dir", "desc")
    sort_sql = {
        ("title", "asc"): "title COLLATE NOCASE ASC",
        ("title", "desc"): "title COLLATE NOCASE DESC",
        ("rating", "asc"): "rating IS NULL, rating ASC",
        ("rating", "desc"): "rating DESC",
    }.get((sort, d), "COALESCE(last_read_at, added_at) DESC")
    with db() as con:
        books = con.execute(f"SELECT * FROM books ORDER BY {sort_sql}").fetchall()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "books": books,
            "sort": sort,
            "dir": d,
            "imported": request.query_params.get("imported"),
            "added": request.query_params.get("added"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/stats")
def stats(request: Request):
    with db() as con:
        totals = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total_seconds),0) secs, COALESCE(SUM(pages_read),0) pages, AVG(rating) avg_rating FROM books"
        ).fetchone()
        days = con.execute(
            "SELECT day, seconds, pages FROM days ORDER BY day"
        ).fetchall()
        top = con.execute(
            "SELECT id, title, author, total_seconds FROM books WHERE total_seconds > 0 ORDER BY total_seconds DESC LIMIT 5"
        ).fetchall()
    max_secs = max((d["seconds"] for d in days), default=0) or 1
    best = max(days, key=lambda r: r["pages"], default=None)
    longest = max(days, key=lambda r: r["seconds"], default=None)
    fmt = lambda day: f"{int(day[8:10])} {date.fromisoformat(day).strftime('%b %Y')}"
    day_map = {r["day"]: (r["seconds"], r["pages"]) for r in days}
    d = date.today() - timedelta(days=364)
    d -= timedelta(days=(d.weekday() + 1) % 7)
    end = date.today() + timedelta(days=(6 - date.today().weekday()) % 7)
    weeks, cur_m, last_lbl = [], None, -9
    while d <= end:
        wk, start = [], d
        for _ in range(7):
            hit = day_map.get(d.isoformat())
            alpha = round(max(0.25, hit[0] / max_secs), 2) if hit else 0
            wk.append((d.strftime("%-d %b %Y"), hit[0], hit[1], alpha) if hit else None)
            d += timedelta(days=1)
        name = start.strftime("%b %Y") if start.month != cur_m and len(weeks) - last_lbl >= 3 else None
        if name:
            last_lbl = len(weeks)
        cur_m = start.month
        weeks.append((wk, name))
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "totals": totals,
            "weeks": weeks,
            "top": top,
            "best": (best["pages"], fmt(best["day"])) if best else None,
            "longest": (
                (round(longest["seconds"] / 3600, 1), fmt(longest["day"]))
                if longest
                else None
            ),
        },
    )


@app.get("/books/{book_id}")
def book_detail(request: Request, book_id: int):
    with db() as con:
        book = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            return RedirectResponse("/", 303)
        agg = con.execute(
            "SELECT COUNT(*) n, SUM(seconds) secs, MIN(day) first_day, MAX(day) last_day FROM book_days WHERE book_id=?",
            (book_id,),
        ).fetchone()
        best = con.execute(
            "SELECT day, seconds FROM book_days WHERE book_id=? ORDER BY seconds DESC LIMIT 1",
            (book_id,),
        ).fetchone()
        anns = con.execute(
            "SELECT * FROM annotations WHERE book_id=? ORDER BY pageno, datetime",
            (book_id,),
        ).fetchall()
    return templates.TemplateResponse(
        request, "book.html", {"b": book, "agg": agg, "best": best, "anns": anns}
    )


@app.post("/books")
def add_book(
    title: str = Form(...),
    author: str = Form(""),
    isbn: str = Form(""),
    pages: int = Form(0),
    rating: float = Form(0),
    review: str = Form(""),
):
    rating = round(rating * 2) / 2
    isbn = re.sub(r"[^0-9Xx]", "", isbn)
    meta = fetch_metadata(isbn) if isbn else {}
    total_pages = pages or meta.get("pages")
    with db() as con:
        con.execute(
            "INSERT INTO books (title, author, isbn, pages_read, total_pages, rating, review, added_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                title or meta.get("title"),
                author or meta.get("author") or None,
                isbn or None,
                total_pages or 0,
                total_pages,
                rating or None,
                review or None,
                int(time.time()),
            ),
        )
    return RedirectResponse("/?added=1", 303)


@app.post("/books/{book_id}/rate")
def rate_book(book_id: int, rating: float = Form(0), review: str = Form("")):
    rating = round(rating * 2) / 2
    with db() as con:
        con.execute(
            "UPDATE books SET rating=?, review=? WHERE id=?",
            (rating or None, review or None, book_id),
        )
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/{book_id}/isbn")
def set_isbn(book_id: int, isbn: str = Form(...)):
    isbn = re.sub(r"[^0-9Xx]", "", isbn)
    with db() as con:
        con.execute(
            "UPDATE books SET isbn=? WHERE id=?",
            (isbn or None, book_id),
        )
        meta = fetch_metadata(isbn) if isbn else {}
        con.execute(
            "UPDATE books SET total_pages=coalesce(total_pages,?), title=coalesce(title,?), author=coalesce(author,?) WHERE id=?",
            (meta.get("pages"), meta.get("title"), meta.get("author"), book_id),
        )
        con.execute(
            "UPDATE books SET pages_read=total_pages WHERE id=? AND pages_read=0 AND total_seconds=0 AND total_pages IS NOT NULL",
            (book_id,),
        )
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int):
    with db() as con:
        con.execute("DELETE FROM books WHERE id=?", (book_id,))
    return RedirectResponse("/", 303)


@app.get("/db/download")
def download_db():
    # ponytail: raw file copy, corrupt if downloaded mid-import; sqlite backup API if that bites
    return FileResponse(DB_PATH, filename="athenaeum.db")


def rebuild_aggregates(con):
    con.execute("DELETE FROM days")
    con.execute("DELETE FROM book_days")
    con.execute(
        "INSERT INTO days (day, seconds, pages) "
        "SELECT date(start_time, 'unixepoch', 'localtime'), SUM(duration), COUNT(DISTINCT page) "
        "FROM sessions GROUP BY 1"
    )
    con.execute(
        "INSERT INTO book_days (book_id, day, seconds, pages) "
        "SELECT book_id, date(start_time, 'unixepoch', 'localtime'), SUM(duration), COUNT(DISTINCT page) "
        "FROM sessions GROUP BY 1, 2"
    )


@app.post("/api/plugin/device")
def plugin_device():
    # ponytail: device registry stub, plugin errors out if this 404s
    return {"message": "Device registered successfully"}


@app.post("/api/plugin/import")
async def plugin_import(request: Request):
    # ponytail: accepts only the stock koinsight.koplugin payload; format drift = 400, no compat layer
    body = await request.json()
    if body.get("version") != "0.3.0":
        return JSONResponse({"error": "Unsupported plugin version, need 0.3.0"}, status_code=400)
    created = 0
    anns = body.get("annotations") or {}
    with db() as con:
        for book in body.get("books") or []:
            md5 = book.get("md5")
            if not md5:
                continue
            title = " ".join((book.get("title") or "").split()) or "Untitled"
            authors = " ".join((book.get("authors") or "").replace(";", ",").split()) or None
            row = find_book(con, title, authors, md5)
            if row:
                bid = row["id"]
                con.execute(
                    "UPDATE books SET md5=?, title=?, author=coalesce(nullif(?, ''), author), "
                    "total_seconds=max(coalesce(total_seconds,0),coalesce(?,0)), "
                    "pages_read=max(coalesce(pages_read,0),coalesce(?,0)), "
                    "total_pages=coalesce(?,total_pages), "
                    "last_read_at=max(coalesce(last_read_at,0),coalesce(?,0)) WHERE id=?",
                    (md5, title, authors, book.get("total_read_time"), book.get("total_read_pages"), book.get("pages"), book.get("last_open"), bid),
                )
            else:
                created += 1
                cur = con.execute(
                    "INSERT INTO books (title, author, md5, total_seconds, pages_read, total_pages, last_read_at, added_at) VALUES (?,?,?,?,?,?,?,?)",
                    (title, authors, md5, book.get("total_read_time") or 0, book.get("total_read_pages") or 0, book.get("pages"), book.get("last_open"), int(time.time())),
                )
                bid = cur.lastrowid
            for s_row in body.get("stats") or []:
                if s_row.get("book_md5") != md5:
                    continue
                if not s_row.get("start_time") or not s_row.get("duration"):
                    continue
                con.execute(
                    "INSERT INTO sessions (book_id, page, start_time, duration, total_pages) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(book_id, page, start_time) DO UPDATE SET duration=excluded.duration, total_pages=excluded.total_pages",
                    (bid, s_row.get("page") or 0, s_row.get("start_time"), s_row.get("duration"), s_row.get("total_pages") or 0),
                )
            for a in anns.get(md5, []):
                insert_annotation(con, bid, a)
        rebuild_aggregates(con)
    return {"message": "Upload successful", "created": created}


@app.post("/import")
def import_koreader(file: UploadFile):
    data = file.file.read()
    try:
        stats = parse_koreader(data)
    except ValueError as e:
        return RedirectResponse(f"/?error={e}", 303)
    created = 0
    with db() as con:
        for s in stats:
            row = find_book(con, s.title, s.author, s.md5)
            if row:
                bid = row["id"]
                con.execute(
                    "UPDATE books SET md5=?, total_seconds=?, pages_read=?, total_pages=coalesce(?,total_pages), last_read_at=coalesce(?,last_read_at) WHERE id=?",
                    (s.md5, s.total_seconds, s.pages_read, s.total_pages, s.last_read_at, bid),
                )
            else:
                created += 1
                cur = con.execute(
                    "INSERT INTO books (title, author, md5, total_seconds, pages_read, total_pages, last_read_at, added_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        s.title,
                        s.author,
                        s.md5,
                        s.total_seconds,
                        s.pages_read,
                        s.total_pages,
                        s.last_read_at,
                        int(time.time()),
                    ),
                )
                bid = cur.lastrowid
            for page, start, dur, tp in s.sessions:
                con.execute(
                    "INSERT INTO sessions (book_id, page, start_time, duration, total_pages) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(book_id, page, start_time) DO UPDATE SET duration=excluded.duration, total_pages=excluded.total_pages",
                    (bid, page, start, dur, tp),
                )
        rebuild_aggregates(con)
    return RedirectResponse(f"/?imported={created}", 303)


def ann_type(a: dict) -> str:
    if not a.get("drawer") and not a.get("color") and not a.get("pos0") and not a.get("pos1"):
        return "bookmark"
    if a.get("note") and a.get("text"):
        return "note"
    return "highlight"


def insert_annotation(con, book_id: int, a: dict):
    con.execute(
        "INSERT INTO annotations (book_id, datetime, page_ref, type, text, note, chapter, pageno, total_pages, color) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(book_id, datetime, page_ref) DO UPDATE SET "
        "type=excluded.type, text=excluded.text, note=excluded.note, chapter=excluded.chapter, color=excluded.color",
        (
            book_id,
            a.get("datetime"),
            str(a.get("page")),
            ann_type(a),
            a.get("text"),
            a.get("note"),
            a.get("chapter"),
            a.get("pageno"),
            a.get("total_pages"),
            a.get("color"),
        ),
    )


def fetch_metadata(isbn: str) -> dict:
    try:
        r = httpx.get(
            "https://openlibrary.org/search.json",
            params={
                "q": f"isbn:{isbn}",
                "fields": "title,author_name,number_of_pages_median",
                "limit": 1,
            },
            timeout=10,
        )
        docs = r.json().get("docs", []) if r.status_code == 200 else []
        if not docs:
            return {}
        return {
            "title": docs[0].get("title"),
            "author": ", ".join(docs[0].get("author_name", [])) or None,
            "pages": docs[0].get("number_of_pages_median") or None,
        }
    except httpx.HTTPError:
        return {}

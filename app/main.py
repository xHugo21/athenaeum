import calendar
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import sqlite3
import tempfile
import time
import zlib
from contextlib import asynccontextmanager
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .koreader import norm, parse_koreader

mimetypes.add_type("application/manifest+json", ".webmanifest")

DB_PATH = os.environ.get("ATHENAEUM_DB", "athenaeum.db")
COVERS_DIR = os.path.join(os.path.dirname(DB_PATH), "covers")
os.makedirs(COVERS_DIR, exist_ok=True)


def cover_path(book_id: int) -> str:
    return os.path.join(COVERS_DIR, f"{book_id}.jpg")


def drop_cover(book_id: int):
    path = cover_path(book_id)
    if os.path.exists(path):
        os.remove(path)


PASSWORD = os.environ.get("ATHENAEUM_PASSWORD")
SESSION_COOKIE = "athenaeum_session"
DATE_FMT = "%-d %b %Y"
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates"),
    context_processors=[lambda request: {"session": bool(PASSWORD and request.cookies.get(SESSION_COOKIE))}],
)


def ts_date(ts: int) -> str:
    return time.strftime(DATE_FMT, time.localtime(ts))


templates.env.filters["ts_date"] = ts_date
templates.env.filters["day_fmt"] = lambda d: date.fromisoformat(d).strftime(DATE_FMT)
templates.env.filters["comma"] = lambda n: f"{int(n):,}"
def read_hm(s: int) -> str:
    h, m = s // 3600, s % 3600 // 60
    if h and m:
        return f"{h}h {m:02d}min"
    if h:
        return f"{h}h"
    return f"{m}min"
templates.env.filters["read_hm"] = lambda s: read_hm(int(s))



def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
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
    cover_failed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'reading',
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
CREATE TABLE IF NOT EXISTS tbr (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS book_labels (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (book_id, label_id)
);
CREATE INDEX IF NOT EXISTS idx_books_md5 ON books(md5);
CREATE INDEX IF NOT EXISTS idx_book_labels_label ON book_labels(label_id);
"""


def migrate(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    if "status" not in cols:
        con.execute("ALTER TABLE books ADD COLUMN status TEXT NOT NULL DEFAULT 'reading'")
        if "finished" not in cols:
            con.execute("UPDATE books SET status='finished' WHERE md5 IS NULL")
        if {"total_pages", "pages_read"} <= cols:
            con.execute(
                "UPDATE books SET status='finished' WHERE total_pages IS NOT NULL AND pages_read >= total_pages"
            )
    if "finished" in cols:
        con.execute("UPDATE books SET status='finished' WHERE finished=1")
        con.execute("ALTER TABLE books DROP COLUMN finished")


@asynccontextmanager
async def lifespan(app):
    with db() as con:
        con.executescript(SCHEMA)
        migrate(con)
    yield

app = FastAPI(title="athenaeum", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.middleware("http")
async def static_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def make_token(nonce: str, expires: int) -> str:
    sig = hmac.new(PASSWORD.encode(), f"{expires}|{nonce}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}|{nonce}|{sig}"


def check_token(token: str) -> bool:
    try:
        expires, nonce, sig = token.split("|")
        if time.time() > int(expires):
            return False
    except ValueError:
        return False
    return hmac.compare_digest(make_token(nonce, int(expires)), token)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if PASSWORD and path not in ("/login", "/api/plugin/device", "/api/plugin/import") and not path.startswith("/static/"):
        token = request.cookies.get(SESSION_COOKIE, "")
        if not check_token(token):
            return RedirectResponse("/login", 303)
    return await call_next(request)


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("error")})


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if not secrets.compare_digest(password, PASSWORD or ""):
        return RedirectResponse("/login?error=Wrong+password", 303)
    expires = int(time.time()) + 30 * 86400
    token = make_token(secrets.token_hex(16), expires)
    res = RedirectResponse("/", 303)
    res.set_cookie(SESSION_COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax")
    return res


@app.post("/logout")
def logout():
    res = RedirectResponse("/login", 303)
    res.delete_cookie(SESSION_COOKIE)
    return res


def clean_isbn(isbn: str) -> str:
    return re.sub(r"[^0-9Xx]", "", isbn)


def snap_rating(rating: float) -> float:
    return round(rating * 2) / 2


def book_index(con):
    by_md5, by_key = {}, {}
    for r in con.execute("SELECT id, title, author, md5 FROM books"):
        entry = dict(r)
        if entry["md5"]:
            by_md5[entry["md5"]] = entry
        by_key.setdefault((norm(entry["title"]), norm(entry["author"] or "")), entry)
    return by_md5, by_key


def find_book(index, title: str, author: str | None, md5: str | None = None):
    by_md5, by_key = index
    if md5 and md5 in by_md5:
        return by_md5[md5]
    return by_key.get((norm(title), norm(author or "")))


def remember_book(index, book_id: int, title: str, author: str | None, md5: str | None):
    by_md5, by_key = index
    for key, entry in list(by_key.items()):
        if entry["id"] == book_id:
            del by_key[key]
    entry = {"id": book_id, "title": title, "author": author, "md5": md5}
    by_key[(norm(title), norm(author or ""))] = entry
    if md5:
        by_md5[md5] = entry


@app.get("/")
def index(request: Request):
    sort = request.query_params.get("sort", "recent")
    d = request.query_params.get("dir", "desc")
    if d not in ("asc", "desc"):
        d = "desc"
    label = request.query_params.get("label", "")
    search = request.query_params.get("q", "").strip()[:200].lower()
    progress = request.query_params.get("progress", "all")
    if progress not in ("all", "reading", "finished", "abandoned"):
        progress = "all"
    rating = request.query_params.get("rating", "any")
    if rating not in ("any", "none", "1", "2", "3", "4", "5"):
        rating = "any"
    sort_sql = {
        ("recent", "asc"): "COALESCE(last_read_at, added_at) ASC",
        ("recent", "desc"): "COALESCE(last_read_at, added_at) DESC",
        ("title", "asc"): "title COLLATE NOCASE ASC",
        ("title", "desc"): "title COLLATE NOCASE DESC",
        ("author", "asc"): "author IS NULL, author COLLATE NOCASE ASC",
        ("author", "desc"): "author IS NULL, author COLLATE NOCASE DESC",
        ("rating", "asc"): "rating IS NULL, rating ASC",
        ("rating", "desc"): "rating DESC",
        ("time", "asc"): "total_seconds ASC",
        ("time", "desc"): "total_seconds DESC",
        ("pages", "asc"): "pages_read ASC",
        ("pages", "desc"): "pages_read DESC",
        ("added", "asc"): "added_at ASC",
        ("added", "desc"): "added_at DESC",
    }.get((sort, d), "COALESCE(last_read_at, added_at) DESC")
    with db() as con:
        books = [dict(r) for r in con.execute(f"SELECT * FROM books ORDER BY {sort_sql}").fetchall()]
        book_labels, label_counts = {}, {}
        for r in con.execute(
            "SELECT bl.book_id, l.name FROM book_labels bl JOIN labels l ON l.id=bl.label_id "
            "ORDER BY l.name COLLATE NOCASE"
        ):
            book_labels.setdefault(r["book_id"], []).append(r["name"])
            label_counts[r["name"]] = label_counts.get(r["name"], 0) + 1
        for b in books:
            b["hue"] = zlib.crc32(b["title"].encode()) % 360
            b["labels"] = book_labels.get(b["id"], [])
    active_label = next((name for name in label_counts if name.casefold() == label.casefold()), None)
    cover_ts = {}
    for b in books:
        p = cover_path(b["id"])
        if os.path.exists(p):
            cover_ts[b["id"]] = int(os.path.getmtime(p))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "books": books,
            "sort": sort,
            "dir": d,
            "search": search,
            "progress": progress,
            "rating": rating,
            "imported": request.query_params.get("imported"),
            "added": request.query_params.get("added"),
            "error": request.query_params.get("error"),
            "cover_ts": cover_ts,
            "labels": label_counts,
            "active_label": active_label,
        },
    )


@app.get("/stats")
def stats(request: Request):
    with db() as con:
        totals = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total_seconds),0) secs, COALESCE(SUM(pages_read),0) pages,"
            " COALESCE(SUM(CASE WHEN md5 IS NOT NULL THEN pages_read END),0) tracked_pages, AVG(rating) avg_rating FROM books"
        ).fetchone()
        days = con.execute(
            "SELECT day, seconds, pages FROM days ORDER BY day"
        ).fetchall()
        top = con.execute(
            "SELECT id, title, author, total_seconds FROM books WHERE total_seconds > 0 ORDER BY total_seconds DESC LIMIT 5"
        ).fetchall()
        hour_secs = [0] * 24
        for h, secs in con.execute(
            "SELECT CAST(strftime('%H', start_time, 'unixepoch', 'localtime') AS INTEGER), SUM(duration) FROM sessions GROUP BY 1"
        ):
            hour_secs[h] = secs
    max_secs = max((d["seconds"] for d in days), default=0) or 1
    best = max(days, key=lambda r: r["pages"], default=None)
    longest = max(days, key=lambda r: r["seconds"], default=None)
    month_secs, wd_secs, wd_count = {}, [0] * 7, [0] * 7
    for r in days:
        month_secs[r["day"][:7]] = month_secs.get(r["day"][:7], 0) + r["seconds"]
        wd = date.fromisoformat(r["day"]).weekday()
        wd_secs[wd] += r["seconds"]
        wd_count[wd] += 1
    m, months = date.today().replace(day=1), []
    for _ in range(12):
        months.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    months.reverse()
    month_bars = [(mo.strftime("%b"), month_secs.get(mo.isoformat()[:7], 0)) for mo in months]
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd_bars = [(lbl, int(wd_secs[i] // wd_count[i]) if wd_count[i] else 0) for i, lbl in enumerate(day_names)]
    hour_bars = [(h, f"{h:02d}:00 – {h + 1:02d}:00 · {read_hm(s)}") for h, s in enumerate(hour_secs)]
    peak_hour = hour_secs.index(max(hour_secs))
    max_hour = hour_secs[peak_hour] or 1
    xs = [(i + 0.5) * 100 / 24 for i in range(24)]
    pts = [(x, 100 - 100 * v / max_hour) for x, v in zip(xs, hour_secs)]
    profile = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
    profile_area = f"M{pts[0][0]:.2f},100 " + " ".join(f"L{x:.2f},{y:.2f}" for x, y in pts) + f" L{pts[-1][0]:.2f},100 Z"
    have = {r["day"] for r in days if r["seconds"] > 0}
    cur_streak, d0 = 0, date.today()
    if d0.isoformat() not in have:
        d0 -= timedelta(days=1)
    while d0.isoformat() in have:
        cur_streak += 1
        d0 -= timedelta(days=1)
    best_streak = run = 0
    prev = None
    for day in sorted(have):
        dd = date.fromisoformat(day)
        run = run + 1 if prev and (dd - prev).days == 1 else 1
        best_streak = max(best_streak, run)
        prev = dd
    max_month = max((s for _, s in month_bars), default=0) or 1
    max_wd = max(s for _, s in wd_bars) or 1
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
            wk.append((d.strftime(DATE_FMT), hit[0], hit[1], alpha) if hit else None)
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
                (longest["seconds"], fmt(longest["day"]))
                if longest
                else None
            ),
            "month_bars": month_bars,
            "max_month": max_month,
            "max_hour": max_hour,
            "peak_hour": peak_hour,
            "hour_bars": hour_bars,
            "profile": profile,
            "profile_area": profile_area,
            "wd_bars": wd_bars,
            "max_wd": max_wd,
            "cur_streak": cur_streak,
            "best_streak": best_streak,
        },
    )


@app.get("/calendar")
def calendar_page(request: Request):
    today = date.today()
    month = request.query_params.get("month", "")
    try:
        first = date.fromisoformat(month + "-01").replace(day=1)
    except ValueError:
        first = today.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    with db() as con:
        rows = con.execute(
            "SELECT bd.day, bd.book_id, b.title, b.author, bd.seconds, bd.pages "
            "FROM book_days bd JOIN books b ON b.id=bd.book_id "
            "WHERE bd.day >= ? AND bd.day < ? "
            "ORDER BY bd.day, bd.seconds DESC, b.title COLLATE NOCASE",
            (first.isoformat(), next_month.isoformat()),
        ).fetchall()
    by_day = {}
    for r in rows:
        day = by_day.setdefault(r["day"], {"books": [], "seconds": 0, "pages": 0})
        day["books"].append(r)
        day["seconds"] += r["seconds"]
        day["pages"] += r["pages"]
    book_days = {}
    for r in rows:
        book = book_days.setdefault(r["book_id"], {"title": r["title"], "dates": set()})
        book["dates"].add(date.fromisoformat(r["day"]))
    runs = []
    for book_id, book in book_days.items():
        dates = sorted(book["dates"])
        start = prev = dates[0]
        for d in dates[1:]:
            if (d - prev).days == 1:
                prev = d
            else:
                runs.append((book_id, book["title"], start, prev))
                start = prev = d
        runs.append((book_id, book["title"], start, prev))
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        cells = []
        for d in week:
            info = by_day.get(d.isoformat()) if d.month == first.month else None
            cells.append(
                {
                    "date": d,
                    "in_month": d.month == first.month,
                    "seconds": info["seconds"] if info else 0,
                    "pages": info["pages"] if info else 0,
                    "books": info["books"] if info else [],
                }
            )
        segments = []
        for book_id, title, start_d, end_d in runs:
            seg_start = max(start_d, week[0])
            seg_end = min(end_d, week[-1])
            if seg_start > seg_end:
                continue
            segments.append(
                {
                    "book_id": book_id,
                    "title": title,
                    "start": (seg_start - week[0]).days,
                    "length": (seg_end - seg_start).days + 1,
                    "continues_left": start_d < week[0],
                    "continues_right": end_d > week[-1],
                }
            )
        segments.sort(key=lambda s: (s["start"], -s["length"], s["title"].casefold()))
        lane_ends = []
        for seg in segments:
            for i, end in enumerate(lane_ends):
                if end < seg["start"]:
                    seg["lane"] = i
                    lane_ends[i] = seg["start"] + seg["length"] - 1
                    break
            else:
                seg["lane"] = len(lane_ends)
                lane_ends.append(seg["start"] + seg["length"] - 1)
        weeks.append({"cells": cells, "events": segments, "extra": max(0, len(lane_ends) - 1)})
    selected = request.query_params.get("day", "")
    try:
        selected_date = date.fromisoformat(selected)
    except ValueError:
        selected_date = None
    if not selected_date or (selected_date.year, selected_date.month) != (first.year, first.month):
        selected = (
            today.isoformat()
            if (today.year, today.month) == (first.year, first.month)
            else (max(by_day) if by_day else "")
        )
    info = by_day.get(selected, {"books": [], "seconds": 0, "pages": 0})
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "month_name": first.strftime("%B %Y"),
            "month_key": first.strftime("%Y-%m"),
            "prev_month": prev_month.strftime("%Y-%m"),
            "next_month": next_month.strftime("%Y-%m"),
            "weeks": weeks,
            "days_read": len(by_day),
            "month_seconds": sum(d["seconds"] for d in by_day.values()),
            "month_pages": sum(d["pages"] for d in by_day.values()),
            "books_read": len({r["book_id"] for d in by_day.values() for r in d["books"]}),
            "selected_day": selected,
            "selected_books": info["books"],
            "selected_seconds": info["seconds"],
            "selected_pages": info["pages"],
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
        sessions_n = con.execute("SELECT COUNT(*) FROM sessions WHERE book_id=?", (book_id,)).fetchone()[0]
        speed = con.execute(
            "SELECT SUM(duration) secs, COUNT(DISTINCT page) pages FROM sessions WHERE book_id=?", (book_id,)
        ).fetchone()
        pace = con.execute(
            "SELECT SUM(seconds)/14.0 FROM book_days WHERE book_id=? AND day>=date('now','-14 day')",
            (book_id,),
        ).fetchone()[0]
        best = con.execute(
            "SELECT day, seconds FROM book_days WHERE book_id=? ORDER BY seconds DESC LIMIT 1",
            (book_id,),
        ).fetchone()
        anns = con.execute(
            "SELECT * FROM annotations WHERE book_id=? ORDER BY pageno, datetime",
            (book_id,),
        ).fetchall()
        labels = [
            r["name"]
            for r in con.execute(
                "SELECT l.name FROM labels l JOIN book_labels bl ON bl.label_id=l.id "
                "WHERE bl.book_id=? ORDER BY l.name COLLATE NOCASE",
                (book_id,),
            )
        ]
    secs_per_page = speed["secs"] / speed["pages"] if speed["secs"] and speed["pages"] >= 2 else None
    left = ((book["total_pages"] or 0) - book["pages_read"]) if book["status"] == "reading" else 0
    time_left = left * secs_per_page if secs_per_page and left > 0 else None
    days_left = round(time_left / pace) if time_left and pace else None
    eta = date.today() + timedelta(days=days_left) if days_left and days_left <= 120 else None
    try:
        cover_ts = int(os.path.getmtime(cover_path(book_id)))
    except OSError:
        cover_ts = 0
    return templates.TemplateResponse(
        request,
        "book.html",
        {
            "b": book,
            "agg": agg,
            "sessions_n": sessions_n,
            "best": best,
            "anns": anns,
            "quotes": [
                {"text": a["text"], "page": a["pageno"]}
                for a in anns
                if a["type"] != "bookmark" and (a["text"] or "").strip()
            ],
            "labels": labels,
            "pph": round(3600 / secs_per_page) if secs_per_page else None,
            "time_left": time_left,
            "eta": eta.strftime("%-d %b") if eta else None,
            "custom_cover": bool(cover_ts),
            "cover_ts": cover_ts,
        },
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
    rating = snap_rating(rating)
    isbn = clean_isbn(isbn)
    meta = fetch_metadata(isbn) if isbn else {}
    total_pages = pages or meta.get("pages")
    with db() as con:
        con.execute(
            "INSERT INTO books (title, author, isbn, pages_read, total_pages, rating, review, status, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                title or meta.get("title"),
                author or meta.get("author") or None,
                isbn or None,
                total_pages or 0,
                total_pages,
                rating or None,
                review or None,
                "finished",
                int(time.time()),
            ),
        )
    return RedirectResponse("/?added=1", 303)


@app.post("/books/{book_id}/rate")
def rate_book(book_id: int, rating: float = Form(0), review: str = Form("")):
    rating = snap_rating(rating)
    with db() as con:
        con.execute(
            "UPDATE books SET rating=?, review=? WHERE id=?",
            (rating or None, review or None, book_id),
        )
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/{book_id}/edit")
def edit_book(
    book_id: int,
    title: str = Form(...),
    author: str = Form(""),
    isbn: str = Form(""),
    pages_read: int = Form(0),
    total_pages: int = Form(0),
):
    isbn = clean_isbn(isbn)
    with db() as con:
        row = con.execute("SELECT isbn, total_seconds FROM books WHERE id=?", (book_id,)).fetchone()
        if not row:
            return RedirectResponse("/", 303)
        meta = fetch_metadata(isbn) if isbn and not row["isbn"] else {}
        new_total = total_pages or meta.get("pages")
        new_read = pages_read
        if new_total and not row["total_seconds"] and not pages_read:
            new_read = new_total
        if isbn and isbn != row["isbn"]:
            con.execute("UPDATE books SET cover_failed=0 WHERE id=?", (book_id,))
        con.execute(
            "UPDATE books SET title=?, author=?, isbn=?, pages_read=?, total_pages=? WHERE id=?",
            (
                title.strip() or None,
                author.strip() or None,
                isbn or None,
                max(0, new_read),
                new_total,
                book_id,
            ),
        )
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/{book_id}/status")
def set_status(book_id: int, status: str = Form("reading")):
    if status not in ("reading", "finished", "abandoned"):
        status = "reading"
    with db() as con:
        con.execute("UPDATE books SET status=? WHERE id=?", (status, book_id))
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int):
    with db() as con:
        con.execute("DELETE FROM books WHERE id=?", (book_id,))
    drop_cover(book_id)
    return RedirectResponse("/", 303)


def clean_label(name: str) -> str:
    return " ".join(name.split())


def attach_label(con, name: str, book_ids: list[int]):
    con.execute("INSERT INTO labels (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    ph = ",".join("?" * len(book_ids))
    con.execute(
        f"INSERT OR IGNORE INTO book_labels (book_id, label_id) "
        f"SELECT id, (SELECT id FROM labels WHERE name=?) FROM books WHERE id IN ({ph})",
        (name, *book_ids),
    )


@app.post("/books/{book_id}/labels")
def set_labels(book_id: int, labels: str = Form("")):
    names = {}
    for raw in labels.split(","):
        name = clean_label(raw)
        if name:
            names.setdefault(name.casefold(), name)
    with db() as con:
        if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
            return RedirectResponse("/", 303)
        con.execute("DELETE FROM book_labels WHERE book_id=?", (book_id,))
        for name in names.values():
            attach_label(con, name, [book_id])
        con.execute("DELETE FROM labels WHERE id NOT IN (SELECT label_id FROM book_labels)")
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/bulk")
def bulk_books(
    ids: str = Form(""),
    action: str = Form(""),
    label: str = Form(""),
    next: str = Form("/"),
):
    book_ids = list(dict.fromkeys(int(x) for x in ids.split(",") if x.strip().isdigit()))[:500]
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    if book_ids:
        ph = ",".join("?" * len(book_ids))
        with db() as con:
            if action == "label":
                name = clean_label(label)
                if name:
                    attach_label(con, name, book_ids)
            elif action in ("reading", "finished", "abandoned"):
                con.execute(
                    f"UPDATE books SET status=? WHERE id IN ({ph})",
                    (action, *book_ids),
                )
            elif action == "delete":
                con.execute(f"DELETE FROM books WHERE id IN ({ph})", tuple(book_ids))
                con.execute("DELETE FROM labels WHERE id NOT IN (SELECT label_id FROM book_labels)")
        if action == "delete":
            for bid in book_ids:
                drop_cover(bid)
    return RedirectResponse(target, 303)


@app.get("/labels")
def labels_page(request: Request):
    with db() as con:
        labels = con.execute(
            "SELECT l.id, l.name, COUNT(bl.book_id) n FROM labels l "
            "LEFT JOIN book_labels bl ON bl.label_id=l.id GROUP BY l.id ORDER BY l.name COLLATE NOCASE"
        ).fetchall()
    return templates.TemplateResponse(request, "labels.html", {"labels": labels})


@app.post("/labels/{label_id}/rename")
def rename_label(label_id: int, name: str = Form("")):
    name = clean_label(name)
    if name:
        with db() as con:
            old = con.execute("SELECT id FROM labels WHERE id=?", (label_id,)).fetchone()
            existing = con.execute(
                "SELECT id FROM labels WHERE name=? AND id<>?", (name, label_id)
            ).fetchone()
            if old and existing:
                con.execute(
                    "INSERT OR IGNORE INTO book_labels (book_id, label_id) "
                    "SELECT book_id, ? FROM book_labels WHERE label_id=?",
                    (existing["id"], label_id),
                )
                con.execute("DELETE FROM labels WHERE id=?", (label_id,))
                con.execute("UPDATE labels SET name=? WHERE id=?", (name, existing["id"]))
            elif old:
                con.execute("UPDATE labels SET name=? WHERE id=?", (name, label_id))
    return RedirectResponse("/labels", 303)


@app.post("/labels/{label_id}/delete")
def delete_label(label_id: int):
    with db() as con:
        con.execute("DELETE FROM labels WHERE id=?", (label_id,))
    return RedirectResponse("/labels", 303)


@app.get("/tbr")
def tbr(request: Request):
    with db() as con:
        books = con.execute("SELECT * FROM tbr ORDER BY added_at DESC, id DESC").fetchall()
    return templates.TemplateResponse(request, "tbr.html", {"books": books})


@app.post("/tbr")
def add_tbr(title: str = Form(...), author: str = Form("")):
    if title.strip():
        with db() as con:
            con.execute(
                "INSERT INTO tbr (title, author, added_at) VALUES (?,?,?)",
                (title.strip(), author.strip() or None, int(time.time())),
            )
    return RedirectResponse("/tbr", 303)


@app.post("/tbr/{book_id}/delete")
def delete_tbr(book_id: int):
    with db() as con:
        con.execute("DELETE FROM tbr WHERE id=?", (book_id,))
    return RedirectResponse("/tbr", 303)


@app.get("/db/download")
def download_db():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(f"VACUUM INTO '{tmp.name}'")
        tmp.seek(0)
        data = tmp.read()
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="athenaeum.db"'},
    )


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


def add_book_row(con, title, author, md5, total_seconds, pages_read, total_pages, last_read_at):
    status = "finished" if total_pages and pages_read >= total_pages else "reading"
    cur = con.execute(
        "INSERT INTO books (title, author, md5, total_seconds, pages_read, total_pages, last_read_at, status, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (title, author, md5, total_seconds, pages_read, total_pages, last_read_at, status, int(time.time())),
    )
    return cur.lastrowid


def upsert_session(con, book_id, page, start_time, duration, total_pages):
    con.execute(
        "INSERT INTO sessions (book_id, page, start_time, duration, total_pages) VALUES (?,?,?,?,?) "
        "ON CONFLICT(book_id, page, start_time) DO UPDATE SET duration=excluded.duration, total_pages=excluded.total_pages",
        (book_id, page, start_time, duration, total_pages),
    )


@app.post("/api/plugin/device")
def plugin_device():
    # ponytail: registry stub on purpose, the plugin only needs a non-404; no device list, dedupe, or revoke.
    # upgrade: fork the plugin and need per-device identity, then build it with the /api/plugin/* auth work.
    return {"message": "Device registered successfully"}


@app.post("/api/plugin/import")
async def plugin_import(request: Request):
    # ponytail: version gate is a floor (>= 0.3.0) on purpose, so later plugin bumps keep importing; deliberately not a v0.3.x ceiling.
    # ceiling: a breaking payload change ships unnoticed, malformed version or JSON raises an unhandled 500.
    # upgrade: fork the plugin, then pin the exact payload version and validate input, with the /api/plugin/* auth work.
    body = await request.json()
    v = body.get("version", "0.0.0").split(".")
    if len(v) != 3 or (int(v[0]), int(v[1])) < (0, 3):
        return JSONResponse({"error": "Unsupported plugin version, need >=0.3.0"}, status_code=400)
    created = 0
    anns = body.get("annotations") or {}
    with db() as con:
        index = book_index(con)
        for book in body.get("books") or []:
            md5 = book.get("md5")
            if not md5:
                continue
            title = " ".join((book.get("title") or "").split()) or "Untitled"
            authors = " ".join((book.get("authors") or "").replace(";", ",").split()) or None
            row = find_book(index, title, authors, md5)
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
                remember_book(index, bid, title, authors or row["author"], md5)
            else:
                created += 1
                bid = add_book_row(
                    con, title, authors, md5, book.get("total_read_time") or 0,
                    book.get("total_read_pages") or 0, book.get("pages"), book.get("last_open"),
                )
                remember_book(index, bid, title, authors, md5)
            for s_row in body.get("stats") or []:
                if s_row.get("book_md5") != md5:
                    continue
                if not s_row.get("start_time") or not s_row.get("duration"):
                    continue
                upsert_session(
                    con, bid, s_row.get("page") or 0, s_row.get("start_time"),
                    s_row.get("duration"), s_row.get("total_pages") or 0,
                )
            for a in anns.get(md5, []):
                insert_annotation(con, bid, a)
        rebuild_aggregates(con)
    return {"message": "Upload successful", "created": created}


@app.post("/import")
def import_koreader(file: UploadFile):
    if file.size and file.size > 20_000_000:
        return RedirectResponse("/?error=File too large", 303)
    data = file.file.read()
    try:
        stats = parse_koreader(data)
    except ValueError as e:
        return RedirectResponse(f"/?error={e}", 303)
    created = 0
    with db() as con:
        index = book_index(con)
        for s in stats:
            row = find_book(index, s.title, s.author, s.md5)
            if row:
                bid = row["id"]
                con.execute(
                    "UPDATE books SET md5=?, title=?, author=coalesce(?,author), total_seconds=?, pages_read=?, total_pages=coalesce(?,total_pages), last_read_at=coalesce(?,last_read_at) WHERE id=?",
                    (s.md5, s.title, s.author, s.total_seconds, s.pages_read, s.total_pages, s.last_read_at, bid),
                )
                remember_book(index, bid, s.title, s.author or row["author"], s.md5)
            else:
                created += 1
                bid = add_book_row(
                    con, s.title, s.author, s.md5, s.total_seconds,
                    s.pages_read, s.total_pages, s.last_read_at,
                )
                remember_book(index, bid, s.title, s.author, s.md5)
            for page, start, dur, tp in s.sessions:
                upsert_session(con, bid, page, start, dur, tp)
        rebuild_aggregates(con)
    return RedirectResponse(f"/?imported={created}", 303)


@app.get("/books/{book_id}/highlights.md")
def download_highlights(book_id: int):
    with db() as con:
        book = con.execute("SELECT title, author FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            return RedirectResponse("/", 303)
        anns = con.execute(
            "SELECT * FROM annotations WHERE book_id=? ORDER BY pageno, datetime",
            (book_id,),
        ).fetchall()
    out = [f"# {book['title']}", ""]
    if book["author"]:
        out.append(f"*{book['author']}*")
        out.append("")
    for a in anns:
        if a["type"] == "bookmark":
            continue
        meta_bits = []
        if a["chapter"]:
            meta_bits.append(a["chapter"])
        meta_bits.append(f"p. {a['pageno'] or '?'}")
        meta = " — ".join(meta_bits)
        for line in (a["text"] or "").splitlines() or [""]:
            out.append(f"> {line}")
        out.append(f">")
        out.append(f"> *— {meta}*")
        if a["note"]:
            out.append(">")
            for i, line in enumerate(a["note"].splitlines()):
                out.append(f"> **Note:** {line}" if i == 0 else f"> {line}")
        out.append("")
    body = "\n".join(out).rstrip() + "\n"
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", book["title"]).strip("_") or "highlights"
    return Response(
        body.encode(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}-highlights.md"'},
    )


@app.get("/books/{book_id}/highlights/edit")
def highlight_form(request: Request, book_id: int, datetime: str = "", page_ref: str = ""):
    with db() as con:
        book = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            return RedirectResponse("/", 303)
        row = con.execute(
            "SELECT * FROM annotations WHERE book_id=? AND datetime=? AND page_ref=?",
            (book_id, datetime, page_ref),
        ).fetchone()
    return templates.TemplateResponse(
        request, "highlight_edit.html", {"b": book, "a": dict(row) if row else {}}
    )


@app.post("/books/{book_id}/highlights/edit")
def highlight_save(
    book_id: int,
    text: str = Form(""),
    note: str = Form(""),
    pageno: str = Form(""),
    chapter: str = Form(""),
    datetime: str = Form(""),
    page_ref: str = Form(""),
):
    a = {
        "text": text.strip() or None,
        "note": note.strip() or None,
        "chapter": chapter.strip() or None,
        "pageno": int(pageno) if pageno.strip().isdigit() else None,
    }
    a["type"] = "bookmark" if not (a["text"] or a["note"]) else "note" if a["note"] else "highlight"
    with db() as con:
        if datetime and page_ref:
            con.execute(
                "UPDATE annotations SET type=?, text=?, note=?, chapter=?, pageno=? "
                "WHERE book_id=? AND datetime=? AND page_ref=?",
                (a["type"], a["text"], a["note"], a["chapter"], a["pageno"], book_id, datetime, page_ref),
            )
        else:
            a["datetime"] = time.strftime("%Y-%m-%d %H:%M:%S")
            a["page"] = "m:" + secrets.token_hex(6)
            insert_annotation(con, book_id, a)
    return RedirectResponse(f"/books/{book_id}#highlights", 303)


@app.post("/books/{book_id}/highlights/delete")
def highlight_delete(book_id: int, datetime: str = Form(...), page_ref: str = Form(...)):
    with db() as con:
        con.execute(
            "DELETE FROM annotations WHERE book_id=? AND datetime=? AND page_ref=?",
            (book_id, datetime, page_ref),
        )
    return RedirectResponse(f"/books/{book_id}#highlights", 303)


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
            a.get("type") or ann_type(a),
            a.get("text"),
            a.get("note"),
            a.get("chapter"),
            a.get("pageno"),
            a.get("total_pages"),
            a.get("color"),
        ),
    )


def fetch_cover(book_id: int, isbn: str | None, failed: int) -> str | None:
    path = cover_path(book_id)
    if os.path.exists(path):
        return path
    if not isbn or failed:
        return None
    try:
        r = httpx.get(
            f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg",
            follow_redirects=True,
            timeout=15,
        )
        if r.status_code == 200 and len(r.content) > 1000:
            with open(path, "wb") as f:
                f.write(r.content)
            return path
    except httpx.HTTPError:
        pass
    with db() as con:
        con.execute("UPDATE books SET cover_failed=1 WHERE id=?", (book_id,))
    return None


@app.get("/covers/{book_id}.jpg")
def cover_image(book_id: int, v: str = ""):
    path = cover_path(book_id)
    cache = {"Cache-Control": "public, max-age=31536000, immutable"} if v else {"Cache-Control": "no-cache"}
    if os.path.exists(path):
        return FileResponse(path, media_type="image/jpeg", headers=cache)
    with db() as con:
        book = con.execute("SELECT isbn, cover_failed FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        return RedirectResponse("/", 303)
    if fetch_cover(book_id, book["isbn"], book["cover_failed"]):
        return FileResponse(path, media_type="image/jpeg", headers=cache)
    return Response(status_code=404)


@app.post("/books/{book_id}/cover/remove")
def remove_cover(book_id: int):
    drop_cover(book_id)
    with db() as con:
        con.execute("UPDATE books SET cover_failed=1 WHERE id=?", (book_id,))
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/{book_id}/cover")
async def upload_cover(book_id: int, cover: UploadFile):
    if cover.content_type and not cover.content_type.startswith("image/"):
        return RedirectResponse(f"/books/{book_id}?error=Not+an+image", 303)
    data = await cover.read()
    if len(data) > 5_000_000 or not data.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"WEBP")):
        return RedirectResponse(f"/books/{book_id}?error=Invalid+image", 303)
    with open(cover_path(book_id), "wb") as f:
        f.write(data)
    return RedirectResponse(f"/books/{book_id}", 303)


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

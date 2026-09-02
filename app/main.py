import os
import re
import sqlite3
import time
import zlib
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
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
templates.env.filters["hours"] = lambda s: f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m"

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
    cover_failed INTEGER NOT NULL DEFAULT 0,
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
        r = con.execute("SELECT id, title, author, md5 FROM books WHERE md5=?", (md5,)).fetchone()
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
        books = [dict(r) for r in con.execute(f"SELECT * FROM books ORDER BY {sort_sql}").fetchall()]
        for b in books:
            b["hue"] = zlib.crc32(b["title"].encode()) % 360
        max_pages = max((b["total_pages"] or 0) for b in books) if books else 1
    cover_ts = {}
    for b in books:
        p = os.path.join(COVERS_DIR, f"{b['id']}.jpg")
        if os.path.exists(p):
            cover_ts[b["id"]] = int(os.path.getmtime(p))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "books": books,
            "max_pages": max_pages or 1,
            "sort": sort,
            "dir": d,
            "imported": request.query_params.get("imported"),
            "added": request.query_params.get("added"),
            "error": request.query_params.get("error"),
            "cover_ts": cover_ts,
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
                (longest["seconds"], fmt(longest["day"]))
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
        request,
        "book.html",
        {
            "b": book,
            "agg": agg,
            "best": best,
            "anns": anns,
            "custom_cover": os.path.exists(os.path.join(COVERS_DIR, f"{book_id}.jpg")),
            "cover_ts": int(os.path.getmtime(os.path.join(COVERS_DIR, f"{book_id}.jpg"))) if os.path.exists(os.path.join(COVERS_DIR, f"{book_id}.jpg")) else 0,
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


@app.post("/books/{book_id}/edit")
def edit_book(
    book_id: int,
    title: str = Form(...),
    author: str = Form(""),
    isbn: str = Form(""),
    pages_read: int = Form(0),
    total_pages: int = Form(0),
):
    isbn = re.sub(r"[^0-9Xx]", "", isbn)
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


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int):
    with db() as con:
        con.execute("DELETE FROM books WHERE id=?", (book_id,))
    return RedirectResponse("/", 303)


@app.get("/db/download")
def download_db():
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(tmp.name) as dst:
            src.backup(dst)
        data = open(tmp.name, "rb").read()
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
    if file.size and file.size > 20_000_000:
        return RedirectResponse("/?error=File too large", 303)
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
                    "UPDATE books SET md5=?, title=?, author=coalesce(?,author), total_seconds=?, pages_read=?, total_pages=coalesce(?,total_pages), last_read_at=coalesce(?,last_read_at) WHERE id=?",
                    (s.md5, s.title, s.author, s.total_seconds, s.pages_read, s.total_pages, s.last_read_at, bid),
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
            for line in a["note"].splitlines():
                out.append(f"> **Note:** {line}" if line == a["note"].splitlines()[0] else f"> {line}")
        out.append("")
    body = "\n".join(out).rstrip() + "\n"
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", book["title"]).strip("_") or "highlights"
    return Response(
        body.encode(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}-highlights.md"'},
    )


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


COVERS_DIR = os.path.join(os.path.dirname(DB_PATH), "covers")
os.makedirs(COVERS_DIR, exist_ok=True)


def fetch_cover(book_id: int, isbn: str | None, failed: int) -> str | None:
    path = os.path.join(COVERS_DIR, f"{book_id}.jpg")
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


def placeholder_svg(title: str, author: str) -> bytes:
    h = abs(hash(title)) % 360
    bg = f"hsl({h},35%,88%)"
    fg = f"hsl({h},45%,30%)"
    initial = (title[:1] or "?").upper()
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;")
    words = esc(title).split()
    lines = []
    for w in words:
        if not lines or len(lines[-1]) + 1 + len(w) > 18:
            lines.append(w)
        else:
            lines[-1] += " " + w
    lines = lines[:4]
    author_safe = esc(author)[:20]
    longest = max((len(l) for l in lines), default=0)
    title_size = 9 if longest <= 14 else 7
    line_h = title_size + 1
    title_y_start = 130 - (len(lines) - 1) * line_h
    title_text = "".join(
        f'<tspan x="64" dy="{line_h if i else 0}">{l}</tspan>' for i, l in enumerate(lines)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 192" preserveAspectRatio="xMidYMid slice">
  <rect width="128" height="192" fill="{bg}"/>
  <rect x="0" y="0" width="128" height="6" fill="{fg}" opacity=".6"/>
  <text x="64" y="84" text-anchor="middle" font-family="Georgia,serif" font-size="48" font-weight="700" fill="{fg}">{initial}</text>
  <text x="64" y="{title_y_start}" text-anchor="middle" font-family="Georgia,serif" font-size="{title_size}" fill="{fg}" opacity=".85">{title_text}</text>
  <text x="64" y="152" text-anchor="middle" font-family="Georgia,serif" font-size="7" fill="{fg}" opacity=".6">{author_safe}</text>
</svg>'''
    return svg.encode()


@app.get("/covers/{book_id}.jpg")
def cover_image(book_id: int):
    with db() as con:
        book = con.execute("SELECT title, author, isbn, cover_failed FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            return RedirectResponse("/", 303)
    path = fetch_cover(book_id, book["isbn"], book["cover_failed"])
    if path:
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    return Response(placeholder_svg(book["title"], book["author"] or ""), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/books/{book_id}/cover/remove")
def remove_cover(book_id: int):
    # ponytail: file existence = "has custom cover"; no DB column, the FS is the source of truth
    path = os.path.join(COVERS_DIR, f"{book_id}.jpg")
    if os.path.exists(path):
        os.remove(path)
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
    path = os.path.join(COVERS_DIR, f"{book_id}.jpg")
    with open(path, "wb") as f:
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

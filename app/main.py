import os
import re
import sqlite3
import time

import httpx
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .koreader import norm, parse_koreader

DB_PATH = os.environ.get("BOOKTRACK_DB", "athenaeum.db")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def ts_date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


templates.env.filters["ts_date"] = ts_date

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
    total_seconds INTEGER NOT NULL DEFAULT 0,
    pages_read INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER,
    last_read_at INTEGER,
    rating INTEGER,
    review TEXT,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS month_seconds (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    month TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    PRIMARY KEY (book_id, month)
);
"""


@app.on_event("startup")
def init():
    with db() as con:
        con.executescript(SCHEMA)


def find_book(con, title: str, author: str | None):
    key = norm(title) + "|" + norm(author or "")
    # ponytail: O(n) scan; fine at personal-library scale, add a normalized column if it ever matters
    for r in con.execute("SELECT id, title, author FROM books"):
        if norm(r["title"]) + "|" + norm(r["author"] or "") == key:
            return r
    return None


@app.get("/")
def index(request: Request):
    with db() as con:
        books = con.execute(
            "SELECT * FROM books ORDER BY COALESCE(last_read_at, added_at) DESC"
        ).fetchall()
        months = con.execute(
            "SELECT month, SUM(seconds) s FROM month_seconds GROUP BY month ORDER BY month DESC LIMIT 12"
        ).fetchall()
    return templates.TemplateResponse(request, "index.html", {"books": books, "months": months})


@app.get("/books/{book_id}")
def book_detail(request: Request, book_id: int):
    with db() as con:
        book = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            return RedirectResponse("/", 303)
        months = con.execute(
            "SELECT month, seconds FROM month_seconds WHERE book_id=? ORDER BY month DESC LIMIT 12",
            (book_id,),
        ).fetchall()
    return templates.TemplateResponse(request, "book.html", {"b": book, "months": months})


@app.post("/books")
def add_book(
    title: str = Form(...),
    author: str = Form(""),
    isbn: str = Form(""),
    rating: int = Form(0),
    review: str = Form(""),
):
    isbn = re.sub(r"[^0-9Xx]", "", isbn)
    meta = fetch_metadata(isbn) if isbn else {}
    with db() as con:
        con.execute(
            "INSERT INTO books (title, author, isbn, rating, review, added_at) VALUES (?,?,?,?,?,?)",
            (
                title or meta.get("title"),
                author or meta.get("author") or None,
                isbn or None,
                rating or None,
                review or None,
                int(time.time()),
            ),
        )
    return RedirectResponse("/", 303)


@app.post("/books/{book_id}/rate")
def rate_book(book_id: int, rating: int = Form(0), review: str = Form("")):
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
    return RedirectResponse(f"/books/{book_id}", 303)


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int):
    with db() as con:
        con.execute("DELETE FROM books WHERE id=?", (book_id,))
    return RedirectResponse("/", 303)


@app.post("/import")
def import_koreader(request: Request, file: UploadFile):
    data = file.file.read()
    try:
        stats = parse_koreader(data)
    except ValueError as e:
        return RedirectResponse(f"/?error={e}", 303)
    created = 0
    with db() as con:
        for s in stats:
            row = find_book(con, s.title, s.author)
            if row:
                bid = row["id"]
                con.execute(
                    "UPDATE books SET total_seconds=?, pages_read=?, total_pages=coalesce(?,total_pages), last_read_at=coalesce(?,last_read_at) WHERE id=?",
                    (s.total_seconds, s.pages_read, s.total_pages, s.last_read_at, bid),
                )
            else:
                created += 1
                isbn = guess_isbn(s.title, s.author)
                cur = con.execute(
                    "INSERT INTO books (title, author, isbn, total_seconds, pages_read, total_pages, last_read_at, added_at) VALUES (?,?,?,?,?,?,?,?)",
                    (s.title, s.author, isbn, s.total_seconds, s.pages_read, s.total_pages, s.last_read_at, int(time.time())),
                )
                bid = cur.lastrowid
            for month, secs in s.months.items():
                con.execute(
                    "INSERT INTO month_seconds (book_id, month, seconds) VALUES (?,?,?) "
                    "ON CONFLICT(book_id, month) DO UPDATE SET seconds=excluded.seconds",
                    (bid, month, secs),
                )
    return RedirectResponse(f"/?imported={created}", 303)


def fetch_metadata(isbn: str) -> dict:
    try:
        r = httpx.get(
            "https://openlibrary.org/search.json",
            params={"q": f"isbn:{isbn}", "fields": "title,author_name", "limit": 1},
            timeout=10,
        )
        docs = r.json().get("docs", []) if r.status_code == 200 else []
        if not docs:
            return {}
        return {
            "title": docs[0].get("title"),
            "author": ", ".join(docs[0].get("author_name", [])) or None,
            "cover": f"https://covers.openlibrary.org/isbn/{isbn}-M.jpg",
        }
    except httpx.HTTPError:
        return {}


def guess_isbn(title: str, author: str | None) -> str | None:
    q = f"title:{title}"
    if author:
        q += f" author:{author}"
    try:
        r = httpx.get(
            "https://openlibrary.org/search.json",
            params={"q": q, "fields": "isbn", "limit": 1, "sort": "editions"},
            timeout=10,
        )
        docs = r.json().get("docs", []) if r.status_code == 200 else []
        if not docs:
            return None
        isbns = docs[0].get("isbn", [])
        return isbns[0] if isbns else None
    except httpx.HTTPError:
        return None

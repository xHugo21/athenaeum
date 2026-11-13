import json
import os
import re
import sqlite3
import threading
from datetime import date, timedelta

import pytest
from app import main
from app.koreader import parse_koreader
from app.main import app, db, migrate
from fastapi.testclient import TestClient


class FakeResp:
    status_code = 200

    def __init__(self, docs):
        self.docs = docs

    def json(self):
        return {"docs": self.docs}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def client(workdir):
    with TestClient(app) as c:
        yield c


def koreader_bytes(
    with_duplicate=False,
    *,
    title="  The   Hobbit ",
    md5="a" * 32,
    seconds=180,
    pages_read=2,
    last_open=1700086400,
) -> bytes:
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE book (
          id INTEGER PRIMARY KEY, title TEXT, authors TEXT, pages INTEGER,
          total_read_time INTEGER, total_read_pages INTEGER, last_open INTEGER, md5 TEXT);
        CREATE TABLE page_stat (id_book INTEGER, page INTEGER, start_time INTEGER, duration INTEGER);
        CREATE TABLE page_stat_data (id_book INTEGER, page INTEGER, start_time INTEGER, duration INTEGER, total_pages INTEGER DEFAULT 0);
        INSERT INTO page_stat VALUES (1, 1, 1700000000, 60);
        INSERT INTO page_stat VALUES (1, 2, 1700086400, 120);
        INSERT INTO page_stat_data VALUES (1, 1, 1700000000, 60, 300);
        INSERT INTO page_stat_data VALUES (1, 2, 1700086400, 120, 300);
        """
    )
    con.execute(
        "INSERT INTO book VALUES (1, ?, 'J.R.R. Tolkien', 300, ?, ?, ?, ?)",
        (title, seconds, pages_read, last_open, md5),
    )
    if with_duplicate:
        con.execute(
            "INSERT INTO book VALUES (2, 'The Hobbit', 'J. r. r. Tolkien', 300, 30, 1, 1700172800, ?)",
            (md5,),
        )
        con.execute("INSERT INTO page_stat VALUES (2, 1, 1700172800, 30)")
        con.execute("INSERT INTO page_stat_data VALUES (2, 1, 1700172800, 30, 300)")
    return bytes(con.serialize())


def test_parse_merges_duplicates():
    stats = parse_koreader(koreader_bytes(with_duplicate=True))
    assert len(stats) == 1, "duplicate book entries should merge"
    s = stats[0]
    assert s.title == "The Hobbit"
    assert s.author == "J.R.R. Tolkien"
    assert s.total_seconds == 210
    assert s.pages_read == 3
    assert s.total_pages == 300
    assert sum(d for _, _, d, _ in s.sessions) == 210


def test_import_and_reimport(client):
    data = koreader_bytes(with_duplicate=True)

    r = client.post("/import", files={"file": ("statistics.sqlite3", data, "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "imported=1" in r.headers["location"]

    r = client.post("/import", files={"file": ("statistics.sqlite3", data, "application/octet-stream")}, follow_redirects=False)
    assert "imported=0" in r.headers["location"]

    r = client.post("/books/1/rate", data={"rating": 5, "review": "nice"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/books/1/rate", data={"rating": 3.5, "review": "nice"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/books/1/rate", data={"rating": 4.7, "review": "nice"}, follow_redirects=False)
    assert r.status_code == 303

    with sqlite3.connect("athenaeum.db") as c:
        (n,) = c.execute("SELECT COUNT(*) FROM books").fetchone()
        assert n == 1, "re-import must not duplicate"
        (title, author, tot, pages) = c.execute(
            "SELECT title, author, total_seconds, pages_read FROM books"
        ).fetchone()
        assert (title, author, tot, pages) == ("The Hobbit", "J.R.R. Tolkien", 210, 3)
        ms = c.execute("SELECT SUM(duration) FROM sessions").fetchone()
        assert ms[0] == 210
        (rating, review) = c.execute("SELECT rating, review FROM books").fetchone()
        assert (rating, review) == (4.5, "nice"), "re-import must keep rating/review; 4.7 snaps to 4.5"


def test_file_reimport_propagates_title_on_md5_match(client):
    first = koreader_bytes(title="  The  Hobbit  ")
    second = koreader_bytes(title="The Hobbit, or There and Back Again")

    r = client.post("/import", files={"file": ("statistics.sqlite3", first, "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 303

    r = client.post("/import", files={"file": ("statistics.sqlite3", second, "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 303
    assert "imported=0" in r.headers["location"], "same md5 = same book, no new row"

    with sqlite3.connect("athenaeum.db") as c:
        (n,) = c.execute("SELECT COUNT(*) FROM books").fetchone()
        assert n == 1
        (title,) = c.execute("SELECT title FROM books").fetchone()
        assert title == "The Hobbit, or There and Back Again", "md5 match must propagate corrected title"


def test_manual_add_with_isbn(client, monkeypatch):
    monkeypatch.setattr(
        main.httpx,
        "get",
        lambda *a, **k: FakeResp([{"title": "Matilda", "author_name": ["Roald Dahl"]}]),
    )
    r = client.post(
        "/books",
        data={"title": "whatever", "author": "", "isbn": "978-0-14-032872-1", "rating": 5, "review": "great"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.post(
        "/books",
        data={"title": "My Book", "author": "Me", "isbn": "978-0-14-032872-1", "pages": "250", "rating": 0, "review": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get("/stats").status_code == 200, "stats must survive a library with no sessions"
    with sqlite3.connect("athenaeum.db") as c:
        row = c.execute("SELECT title, author, isbn, rating FROM books ORDER BY id").fetchall()
    assert row == [
        ("whatever", "Roald Dahl", "9780140328721", 5),
        ("My Book", "Me", "9780140328721", None),
    ]
    (pages,) = sqlite3.connect("athenaeum.db").execute(
        "SELECT total_pages FROM books WHERE title='My Book'"
    ).fetchone()
    assert pages == 250


def test_set_isbn_fills_pages_for_manual_add(client, monkeypatch):
    monkeypatch.setattr(
        main.httpx,
        "get",
        lambda *a, **k: FakeResp(
            [{"title": "Matilda", "author_name": ["Roald Dahl"], "number_of_pages_median": 96}]
        ),
    )
    client.post("/books", data={"title": "Manual Book", "pages": ""}, follow_redirects=False)
    r = client.post("/books/1/edit", data={"title": "Manual Book", "isbn": "9780140328721"}, follow_redirects=False)
    assert r.status_code == 303
    with sqlite3.connect("athenaeum.db") as c:
        (total, read) = c.execute("SELECT total_pages, pages_read FROM books WHERE id=1").fetchone()
    assert (total, read) == (96, 96), "metadata pages must fill NULL total_pages even for manual adds"


def test_stats_page(client):
    client.post("/import", files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")})
    r = client.get("/stats")
    assert r.status_code == 200
    assert "The Hobbit" in r.text
    assert "3m" in r.text
    assert "Monthly reading" in r.text and "Time of day" in r.text
    assert r.text.count('class="hit"') == 24, "every hour needs a hover target"
    assert r.text.count('role="img"') == 2, "two charts only: the hour profile and the heatmap"
    with sqlite3.connect("athenaeum.db") as c:
        hours = c.execute("SELECT COUNT(*), SUM(seconds) FROM hours").fetchone()
        total = c.execute("SELECT SUM(duration) FROM sessions").fetchone()[0]
    assert hours[1] == total, "the hour table is rebuilt from every session"
    assert 0 < hours[0] <= 24
    assert "from KOReader" not in r.text, "all-tracked library needs no breakdown"

    client.post("/books", data={"title": "Manual Book", "pages": 96}, follow_redirects=False)
    r = client.get("/stats")
    assert "2 from KOReader" in r.text, "untracked pages must not count as KOReader ones"


def test_reject_garbage(client):
    r = client.post("/import", files={"file": ("x.sqlite3", b"not a db at all", "application/octet-stream")}, follow_redirects=False)
    assert r.status_code == 303
    assert "error" in r.headers["location"]


def test_plugin_sync_no_dupes(client):
    md5 = "f" * 32
    payload = {
        "version": "0.3.0",
        "books": [{"id": 1, "title": "Moby Dick", "authors": "Herman Melville", "pages": 720,
                   "total_read_time": 5400, "total_read_pages": 120, "last_open": 1700086400, "md5": md5}],
        "stats": [
            {"book_md5": md5, "page": 3, "start_time": 1700000000, "duration": 600, "total_pages": 720},
            {"book_md5": md5, "page": 4, "start_time": 1700086400, "duration": 900, "total_pages": 720},
        ],
        "annotations": {md5: [
            {"datetime": "2024-01-05T10:00:00", "page": 42, "pageno": 42, "total_pages": 720,
             "text": "Call me Ishmael.", "chapter": "Loomings", "drawer": "lighten"},
            {"datetime": "2024-01-07T09:00:00", "page": 100, "pageno": 100, "total_pages": 720,
             "text": "in Loomings", "chapter": "Loomings"},
        ]},
        "device_id": "dev1",
    }
    r = client.post("/api/plugin/device", json={"id": "d1", "model": "kobo"}, follow_redirects=False)
    assert r.status_code == 200, "device endpoint must exist for the plugin"

    r = client.post("/api/plugin/import", json=payload, follow_redirects=False)
    assert r.status_code == 200 and r.json()["created"] == 1

    r = client.post("/api/plugin/import", json=payload, follow_redirects=False)
    assert r.json()["created"] == 0, "re-sync must not duplicate"

    r = client.post("/api/plugin/import", json=dict(payload, version="0.2.9"), follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/api/plugin/import", json=dict(payload, version="0.3.5"), follow_redirects=False)
    assert r.status_code == 200
    r = client.post("/api/plugin/import", json=dict(payload, version="bad"), follow_redirects=False)
    assert r.status_code == 400

    file_import = client.post(
        "/import",
        files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")},
        follow_redirects=False,
    )
    assert file_import.status_code == 303

    with sqlite3.connect("athenaeum.db") as c:
        (secs, pread) = c.execute("SELECT total_seconds, pages_read FROM books WHERE title='Moby Dick'").fetchone()
        assert (secs, pread) == (5400, 120), "plugin sync must store reading totals"
        (n,) = c.execute("SELECT COUNT(*) FROM books").fetchone()
        assert n == 2, "plugin book + file book stay separate (different md5)"
        (n,) = c.execute("SELECT COUNT(*) FROM annotations").fetchone()
        assert n == 2, "re-sync must not duplicate annotations"
        (session_secs,) = c.execute("SELECT SUM(duration) FROM sessions WHERE book_id=1").fetchone()
        assert session_secs == 1500, "plugin stats rows must land in sessions"
        (day_secs,) = c.execute("SELECT SUM(seconds) FROM days").fetchone()
        assert day_secs == 1680, "global stats = plugin sessions + file sessions (1500 + 180)"
        (book_secs,) = c.execute("SELECT SUM(seconds) FROM book_days WHERE book_id=1").fetchone()
        assert book_secs == 1500, "book stats must rebuild from plugin sessions"
        (secs,) = c.execute("SELECT total_seconds FROM books WHERE md5=?", ("a" * 32,)).fetchone()
        assert secs == 180, "manual file import owns its books' stats"

    r = client.get("/books/1")
    assert "Call me Ishmael" in r.text
    r = client.get("/stats")
    assert "Time of day" in r.text


def test_stats_streaks(client):

    base = date.today()
    client.post("/books", data={"title": "T"}, follow_redirects=False)
    con = sqlite3.connect("athenaeum.db")
    for i in range(3):
        con.execute("INSERT INTO days VALUES (?, 60, 1)", ((base - timedelta(days=i)).isoformat(),))
    con.execute("INSERT INTO days VALUES (?, 60, 1)", ((base - timedelta(days=10)).isoformat(),))
    con.commit()
    con.close()
    r = client.get("/stats")
    assert r.status_code == 200
    assert "Monthly reading" in r.text
    assert re.search(r'class="streak current">\s*<div class="n num">3<', r.text)
    assert re.search(r'class="streak best">\s*<div class="n num">3<', r.text)


def test_annotations_export(client):
    client.post("/books", data={"title": "1984", "author": "Orwell"}, follow_redirects=False)
    con = sqlite3.connect("athenaeum.db")
    con.execute(
        "INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, "2024-01-01 10:00:00", "5", "highlight", "It was a bright cold day in April.", None, "Chapter 1", 5, 200, "#ff0000"),
    )
    con.execute(
        "INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, "2024-01-01 11:00:00", "12", "note", "The clocks were striking thirteen.", "Surreal.", "Chapter 2", 12, 200, None),
    )
    con.execute(
        "INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, "2024-01-02 09:00:00", "42", "bookmark", "in Chapter 3", "A bookmark note", "Chapter 3", 42, 200, None),
    )
    con.commit()
    con.close()
    r = client.get("/books/1/annotations.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert 'filename="1984-annotations.md"' in r.headers["content-disposition"]
    body = r.text
    assert "# 1984" in body and "*Orwell*" in body
    assert "## Highlights" in body and "## Bookmarks" in body
    assert "- Chapter 3 · p. 42" in body
    assert "**Note:** A bookmark note" in body
    assert "in Chapter 3" not in body, "a bookmark's text is the chapter label, not a quote"
    assert "> It was a bright cold day in April." in body
    assert "*— Chapter 1 — p. 5*" in body
    assert "> The clocks were striking thirteen." in body
    assert "**Note:** Surreal." in body


def test_delete_book_removes_cover_file(client):
    os.makedirs("covers", exist_ok=True)
    client.post("/import", files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")})
    r = client.post("/books/1/cover", files={"cover": ("c.jpg", b"\xff\xd8\xff" + b"x" * 100, "image/jpeg")}, follow_redirects=False)
    assert r.status_code == 303
    assert os.path.exists("covers/1.jpg"), "upload should write the cover"

    r = client.post("/books/1/delete", follow_redirects=False)
    assert r.status_code == 303

    assert not os.path.exists("covers/1.jpg"), "deleting a book must remove its cover file"
    with sqlite3.connect("athenaeum.db") as c:
        assert c.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def test_tbr_add_and_remove(client):
    r = client.post("/tbr", data={"title": "  Dune  ", "author": "Frank Herbert"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/tbr", data={"title": "  ", "author": "nobody"}, follow_redirects=False)
    r = client.get("/tbr")
    assert r.status_code == 200
    assert "Dune" in r.text and "Frank Herbert" in r.text
    with sqlite3.connect("athenaeum.db") as c:
        rows = c.execute("SELECT title, author FROM tbr").fetchall()
    assert rows == [("Dune", "Frank Herbert")], "blank title must be ignored, title trimmed"
    r = client.post("/tbr/1/delete", follow_redirects=False)
    assert r.status_code == 303
    with sqlite3.connect("athenaeum.db") as c:
        assert c.execute("SELECT COUNT(*) FROM tbr").fetchone()[0] == 0


def test_book_page_shows_sessions_and_days(client):

    client.post("/books", data={"title": "T"}, follow_redirects=False)
    con = sqlite3.connect("athenaeum.db")
    con.execute("INSERT INTO book_days VALUES (1, '2024-01-01', 1800, 40)")
    for i in range(3):
        con.execute("INSERT INTO sessions VALUES (1, ?, ?, 600, 0)", (i, 1704067200 + i))
    con.commit()
    con.close()
    r = client.get("/books/1")
    assert r.status_code == 200

    def stat(label):
        m = re.search(r'class="label">' + label + r'</div>\s*<div class="v num"[^>]*>([^<]+)<', r.text)
        return m.group(1) if m else None

    assert stat("Sessions") == "3", "sessions must count session rows, not days"
    assert stat("Days read") == "1"


def test_md5_lookup_is_indexed(client):
    client.post("/import", files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")})
    with sqlite3.connect("athenaeum.db") as c:
        plan = c.execute("EXPLAIN QUERY PLAN SELECT id FROM books WHERE md5=?", ("x",)).fetchall()
        assert any("idx_books_md5" in r[3] for r in plan), f"md5 lookup must use the index, got {plan}"


def test_manual_highlight_crud(client):
    client.post("/books", data={"title": "Kindle Book", "author": "Someone"}, follow_redirects=False)
    r = client.post(
        "/books/1/annotations/edit",
        data={"text": "A passage", "pageno": "12", "chapter": "One"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/books/1").text
    assert "A passage" in page and "One" in page and "p. 12" in page

    with sqlite3.connect("athenaeum.db") as c:
        datetime, page_ref, kind, total_pages = c.execute(
            "SELECT datetime, page_ref, type, total_pages FROM annotations"
        ).fetchone()
    assert kind == "highlight" and page_ref.startswith("m:") and total_pages is None

    form = client.get(f"/books/1/annotations/edit?datetime={datetime}&page_ref={page_ref}").text
    assert "A passage" in form and "Delete" in form
    assert "Edit highlight" in form and 'name="text"' in form
    r = client.post(
        "/books/1/annotations/edit",
        data={"text": "A passage", "note": "My thought", "pageno": "12", "chapter": "One",
              "datetime": datetime, "page_ref": page_ref},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with sqlite3.connect("athenaeum.db") as c:
        (n,) = c.execute("SELECT COUNT(*) FROM annotations").fetchone()
        kind, note = c.execute("SELECT type, note FROM annotations").fetchone()
    assert n == 1, "editing must update in place"
    assert (kind, note) == ("highlight", "My thought"), "editing never reclassifies an annotation"

    assert "My thought" in client.get("/books/1").text, "typed notes must show on the book page"
    r = client.post(
        "/books/1/annotations/delete",
        data={"datetime": datetime, "page_ref": page_ref},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "No highlights yet" in client.get("/books/1").text

    with sqlite3.connect("athenaeum.db") as c:
        (n,) = c.execute("SELECT COUNT(*) FROM annotations").fetchone()
    assert n == 0


def test_manual_book_status_and_abandoned_filter(client):
    client.post("/books", data={"title": "Bought yesterday"}, follow_redirects=False)
    assert 'data-state="finished"' in client.get("/").text
    book = client.get("/books/1").text
    assert '<option value="finished" selected>' in book and '<span class="tag" id="status-tag">finished</span>' in book

    client.post("/books/1/status", data={"status": "reading"}, follow_redirects=False)
    assert 'data-state="reading"' in client.get("/").text
    assert '<option value="reading" selected>' in client.get("/books/1").text

    client.post("/books/1/status", data={"status": "abandoned"}, follow_redirects=False)
    page = client.get("/?progress=abandoned").text
    assert 'data-state="abandoned"' in page
    assert 'data-value="abandoned" aria-pressed="true"' in page
    assert '<span class="tag tag-quiet" id="status-tag">abandoned</span>' in client.get("/books/1").text


def test_imported_book_can_be_marked_read_short_of_last_page(client):
    client.post("/import", files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")})
    assert 'data-state="reading"' in client.get("/").text
    assert '<option value="reading" selected>' in client.get("/books/1").text

    client.post("/books/1/status", data={"status": "finished"}, follow_redirects=False)
    assert 'data-state="finished"' in client.get("/").text
    book = client.get("/books/1").text
    assert '<option value="finished" selected>' in book and '<span class="tag" id="status-tag">finished</span>' in book


def test_migrate_marks_manual_books_read(workdir):

    con = sqlite3.connect("athenaeum.db")
    con.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, md5 TEXT)")
    con.execute("INSERT INTO books (title, md5) VALUES ('manual', NULL), ('synced', 'abc')")
    migrate(con)
    con.commit()
    rows = dict(con.execute("SELECT title, status FROM books").fetchall())
    assert rows == {"manual": "finished", "synced": "reading"}


def test_migrate_finished_column_to_status(workdir):

    con = sqlite3.connect("athenaeum.db")
    con.execute(
        "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, md5 TEXT, pages_read INTEGER, total_pages INTEGER, finished INTEGER)"
    )
    con.execute("INSERT INTO books (title, md5, pages_read, total_pages, finished) VALUES ('manual', NULL, 0, NULL, 0)")
    con.execute("INSERT INTO books (title, md5, pages_read, total_pages, finished) VALUES ('complete', 'abc', 100, 100, 0)")
    con.execute("INSERT INTO books (title, md5, pages_read, total_pages, finished) VALUES ('marked', 'def', 10, 100, 1)")
    con.execute("INSERT INTO books (title, md5, pages_read, total_pages, finished) VALUES ('reading', 'ghi', 10, 100, 0)")
    migrate(con)
    con.commit()
    rows = dict(con.execute("SELECT title, status FROM books").fetchall())
    assert rows == {"manual": "reading", "complete": "finished", "marked": "finished", "reading": "reading"}
    assert "finished" not in {r[1] for r in con.execute("PRAGMA table_info(books)")}


def test_db_is_wal_and_survives_concurrent_writes(client):

    with db() as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 1

    errors = []

    def writer(i):
        try:
            with db() as con:
                con.execute("INSERT INTO tbr (title, added_at) VALUES (?, ?)", (f"book {i}", i))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with db() as con:
        assert con.execute("SELECT COUNT(*) FROM tbr").fetchone()[0] == 10


def test_book_labels_assign_and_filter(client):

    client.post("/books", data={"title": "Dune"}, follow_redirects=False)
    client.post("/books", data={"title": "Emma"}, follow_redirects=False)
    r = client.post("/books/1/labels", data={"labels": " Sci-Fi, space ,sci-fi,, "}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/books/2/labels", data={"labels": "classic"}, follow_redirects=False)

    page = client.get("/books/1").text
    assert "Sci-Fi" in page and "space" in page
    assert 'href="/?label=Sci-Fi"' in page
    assert 'class="label-x"' in page and 'id="label-add"' in page

    r = client.get("/?label=space")
    data = json.loads(re.search(r'id="book-data" type="application/json">(.*?)</script>', r.text, re.S).group(1))
    assert [b["title"] for b in data] == ["Dune", "Emma"], "label filtering happens in the browser"
    dune = next(b for b in data if b["title"] == "Dune")
    assert sorted(dune["labels"]) == ["Sci-Fi", "space"]
    assert '<button type="button" data-value="space" aria-pressed="true">' in r.text

    client.post("/books/1/labels", data={"labels": "sci-fi, favorite"}, follow_redirects=False)

    with sqlite3.connect("athenaeum.db") as c:
        names = dict(c.execute("SELECT name, id FROM labels").fetchall())
        assert sorted(names) == ["Sci-Fi", "classic", "favorite"], "labels are reused case-insensitively and orphans removed"
        rows = c.execute(
            "SELECT l.name FROM book_labels bl JOIN labels l ON l.id=bl.label_id WHERE bl.book_id=1 ORDER BY l.name"
        ).fetchall()
    assert {r[0] for r in rows} == {"Sci-Fi", "favorite"}


def test_file_inputs_are_keyboard_reachable(client):
    client.post("/books", data={"title": "Dune"}, follow_redirects=False)
    for path in ("/", "/books/1"):
        tags = re.findall(r'<input[^>]*type="file"[^>]*>', client.get(path).text)
        assert tags, path
        assert all("sr-only" in t and "display:none" not in t and not re.search(r"\shidden", t) for t in tags), path


def test_empty_library_keeps_add_panel_working(client):
    html = client.get("/").text
    assert 'id="add-card"' in html
    assert "function panel(" in html, "add and import buttons are dead without books"


def test_base_css_is_cacheable(client):
    m = re.search(r'href="(/static/app\.css\?v=\d+)"', client.get("/").text)
    assert m, "base styles must be a versioned static file"
    r = client.get(m.group(1))
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert "--accent" in r.text, "the file really is the stylesheet"


def test_theme_toggle_reports_its_state(client):
    html = client.get("/").text
    assert 'role="switch"' in html and 'aria-label="Dark mode"' in html
    assert "syncThemeBtn" in html, "initial aria-checked is only corrected by script"
    assert '<main id="content" tabindex="-1">' in html, "skip link must move focus, not just scroll"


def test_library_filters_in_url(client):
    client.post("/books", data={"title": "Dune", "rating": 5}, follow_redirects=False)
    client.post("/books/1/labels", data={"labels": "space"}, follow_redirects=False)
    r = client.get("/?q=Dune&progress=finished&rating=5&label=SPACE")
    assert r.status_code == 200
    assert 'data-value="finished" aria-pressed="true"' in r.text
    assert 'data-value="5" aria-pressed="true"' in r.text
    assert 'data-value="space" aria-pressed="true"' in r.text
    assert 'q: "dune"' in r.text
    assert 'label: "space"' in r.text
    assert 'id="bulk-bar"' in r.text and 'class="book-cell"' in r.text


def test_bulk_actions(client):
    for title in ("A", "B", "C"):
        client.post("/books", data={"title": title}, follow_redirects=False)

    r = client.post(
        "/books/bulk",
        data={"ids": "1,2,1", "action": "label", "label": " Favorites ", "next": "/?progress=finished"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/?progress=finished"

    r = client.post("/books/bulk", data={"ids": "1,2", "action": "reading", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/books/bulk", data={"ids": "1", "action": "abandoned", "next": "/"}, follow_redirects=False)

    r = client.post("/books/bulk", data={"ids": "3", "action": "delete", "next": "//evil.test"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"

    with sqlite3.connect("athenaeum.db") as c:
        assert c.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM labels WHERE name='Favorites'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM book_labels").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM books WHERE status='reading'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM books WHERE status='abandoned'").fetchone()[0] == 1


def test_label_management_rename_merge_delete(client):
    client.post("/books", data={"title": "A"}, follow_redirects=False)
    client.post("/books", data={"title": "B"}, follow_redirects=False)
    client.post("/books/1/labels", data={"labels": "fantasy, space"}, follow_redirects=False)
    client.post("/books/2/labels", data={"labels": "fantasy"}, follow_redirects=False)
    assert client.get("/labels").status_code == 200

    with sqlite3.connect("athenaeum.db") as c:
        ids = dict(c.execute("SELECT name, id FROM labels"))

    r = client.post(f"/labels/{ids['space']}/rename", data={"name": "sci-fi"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/labels"
    r = client.post(f"/labels/{ids['fantasy']}/rename", data={"name": "sci-fi"}, follow_redirects=False)
    assert r.status_code == 303

    page = client.get("/labels").text
    assert "sci-fi" in page and "fantasy" not in page
    with sqlite3.connect("athenaeum.db") as c:
        assert c.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM book_labels").fetchone()[0] == 2
        label_id = c.execute("SELECT id FROM labels").fetchone()[0]

    client.post(f"/labels/{label_id}/delete", follow_redirects=False)

    with sqlite3.connect("athenaeum.db") as c:
        assert c.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM book_labels").fetchone()[0] == 0


def test_library_sorts(client):

    client.post("/books", data={"title": "Zebra", "author": "Alice", "pages": 100}, follow_redirects=False)
    client.post("/books", data={"title": "Alpha", "author": "Bob", "pages": 200}, follow_redirects=False)
    client.post("/books", data={"title": "Middle", "pages": 50}, follow_redirects=False)
    with sqlite3.connect("athenaeum.db") as c:
        c.execute("UPDATE books SET total_seconds=10, pages_read=10, added_at=30 WHERE id=1")
        c.execute("UPDATE books SET total_seconds=300, pages_read=200, added_at=10 WHERE id=2")
        c.execute("UPDATE books SET total_seconds=100, pages_read=50, added_at=20 WHERE id=3")

    def order(url):
        html = client.get(url).text
        return [int(x) for x in re.findall(r'class="book-cell" data-id="(\d+)"', html)]

    assert order("/?sort=title&dir=asc") == [2, 3, 1]
    assert order("/?sort=author&dir=asc") == [1, 2, 3]
    assert order("/?sort=pages&dir=desc") == [2, 3, 1]
    assert order("/?sort=time&dir=desc") == [2, 3, 1]
    assert order("/?sort=added&dir=asc") == [2, 3, 1]


def test_placeholder_cover_uses_html_fallback(client):
    client.post("/books", data={"title": "No Cover", "author": "Nobody"}, follow_redirects=False)
    miss = client.get("/covers/1.jpg", follow_redirects=False)
    assert miss.status_code == 404
    assert "immutable" not in miss.headers.get("cache-control", ""), "a miss must not be cached for a year"
    assert 'class="cover-fallback"' in client.get("/").text
    assert 'class="cover-fallback"' in client.get("/books/1").text
    assert "A·001" not in client.get("/").text


def test_cover_fetch_runs_after_the_response(client, monkeypatch):
    client.post("/import", files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")})
    with sqlite3.connect("athenaeum.db") as c:
        c.execute("UPDATE books SET isbn='9780140328721' WHERE id=1")
    calls = []
    monkeypatch.setattr(main, "fetch_cover", lambda *a: calls.append(a))
    assert client.get("/covers/1.jpg").status_code == 404
    assert calls == [(1, "9780140328721", 0)], "the miss schedules a fetch instead of waiting on it"


def test_calendar_lists_books_per_day(client):
    client.post("/books", data={"title": "Dune", "author": "Frank Herbert"}, follow_redirects=False)
    client.post("/books", data={"title": "Emma", "author": "Jane Austen"}, follow_redirects=False)
    con = sqlite3.connect("athenaeum.db")
    con.execute("INSERT INTO book_days VALUES (1, '2026-11-09', 600, 10)")
    con.execute("INSERT INTO book_days VALUES (1, '2026-11-10', 600, 10)")
    con.execute("INSERT INTO book_days VALUES (1, '2026-11-11', 3600, 120)")
    con.execute("INSERT INTO book_days VALUES (2, '2026-11-11', 1800, 50)")
    con.execute("INSERT INTO book_days VALUES (1, '2026-11-12', 600, 20)")
    con.commit()
    con.close()

    r = client.get("/calendar?month=2026-11&day=2026-11-11")
    assert r.status_code == 200
    assert "November 2026" in r.text
    assert "1h 30min" in r.text and "170" in r.text
    assert 'role="group" aria-label="Reading calendar for November 2026"' in r.text
    assert 'aria-label="Dune, 9 Nov 2026 to 12 Nov 2026"' in r.text, "event names carry their date range"
    assert r.text.index("Dune") < r.text.index("Emma"), "books are ordered by time read"
    assert "grid-column: 1 / span 4" in r.text, "consecutive days share one book bar"
    assert 'aria-current="date"' in r.text
    assert "month=2026-10" in r.text and "month=2026-12" in r.text

    r = client.get("/calendar?month=2026-12")
    assert "December 2026" in r.text
    assert "No reading logged this month." in r.text


def test_static_assets_are_public_behind_password(client, monkeypatch):

    monkeypatch.setattr(main, "PASSWORD", "secret")
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/apple-touch-icon-180.png").status_code == 200
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/covers/1.jpg", follow_redirects=False).status_code == 303


def test_book_reading_pace(client):
    client.post("/import", files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")})
    r = client.get("/books/1")
    assert "pages an hour" in r.text and "Time left" in r.text
    assert "7h 27min" in r.text, "298 pages left at 90s a page"
    assert r.text.count("Reading speed") == 1
    assert client.get("/stats").status_code == 200


def test_book_page_has_story_share(client):
    client.post("/books", data={"title": "Dune", "author": "Frank Herbert", "rating": "4.5"})
    assert 'id="story-quote"' not in client.get("/books/1").text, "no highlights, no picker"
    client.post("/books/1/annotations/edit", data={"text": "Fear is the mind-killer", "pageno": "59"})
    page = client.get("/books/1").text
    assert 'id="story-overlay"' in page and 'id="story-btn"' in page
    assert 'aria-modal="true"' in page and 'tabindex="-1"' in page
    assert '"rating": 4.5' in page and '"title": "Dune"' in page
    assert '<option value="0">p. 59 · Fear is the mind-killer</option>' in page


def test_plugin_resync_keeps_high_water_totals(client):
    md5 = "e" * 32

    def payload(seconds, pages_read, last_open):
        return {
            "version": "0.3.0",
            "books": [{"title": "Moby Dick", "authors": "Herman Melville", "pages": 720,
                       "total_read_time": seconds, "total_read_pages": pages_read,
                       "last_open": last_open, "md5": md5}],
            "stats": [],
        }

    client.post("/api/plugin/import", json=payload(5400, 120, 1700086400), follow_redirects=False)
    client.post("/api/plugin/import", json=payload(100, 5, 1700172800), follow_redirects=False)

    with sqlite3.connect("athenaeum.db") as c:
        row = c.execute("SELECT total_seconds, pages_read, total_pages, last_read_at FROM books").fetchone()
    assert row == (5400, 120, 720, 1700172800), "plugin sync never lowers totals and keeps the latest read date"


def test_file_reimport_replaces_totals(client):
    first = koreader_bytes(seconds=500, pages_read=2, last_open=1700086400)
    second = koreader_bytes(seconds=120, pages_read=1, last_open=1600000000)

    client.post("/import", files={"file": ("statistics.sqlite3", first, "application/octet-stream")}, follow_redirects=False)
    client.post("/import", files={"file": ("statistics.sqlite3", second, "application/octet-stream")}, follow_redirects=False)

    with sqlite3.connect("athenaeum.db") as c:
        row = c.execute("SELECT total_seconds, pages_read, last_read_at FROM books").fetchone()
    assert row == (120, 1, 1600000000), "file import replaces totals, even downwards"


def test_db_backup_is_valid_sqlite(client):
    client.post("/books", data={"title": "Backup Me"}, follow_redirects=False)

    r = client.get("/db/download")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="athenaeum.db"'
    con = sqlite3.connect(":memory:")
    con.deserialize(r.content)
    assert con.execute("SELECT title FROM books").fetchone()[0] == "Backup Me"


def test_db_closes_connection_after_block(workdir):
    with db() as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_db_rolls_back_on_error(client):
    with pytest.raises(RuntimeError):
        with db() as con:
            con.execute("INSERT INTO tbr (title, added_at) VALUES ('ghost', 1)")
            raise RuntimeError("boom")

    with db() as con:
        assert con.execute("SELECT COUNT(*) FROM tbr").fetchone()[0] == 0


def test_plugin_annotation_types_follow_position(client):
    md5 = "d" * 32
    payload = {
        "version": "0.3.0",
        "books": [{"title": "Type Book", "authors": "A", "pages": 100, "md5": md5}],
        "stats": [],
        "annotations": {md5: [
            {"datetime": "2024-01-01T09:00:00", "page": 1, "text": "in Loomings"},
            {"datetime": "2024-01-02T09:00:00", "page": 2, "text": "in Loomings", "note": "a bookmark note"},
            {"datetime": "2024-01-03T09:00:00", "page": 3, "text": "Call me Ishmael.", "drawer": "lighten"},
            {"datetime": "2024-01-04T09:00:00", "page": 4, "text": "Call me Ishmael.", "note": "note", "drawer": "lighten"},
            {"datetime": "2024-01-05T09:00:00", "page": 5, "text": "text without a position"},
            {"datetime": "2024-01-06T09:00:00", "page": 6, "color": "yellow"},
        ]},
    }
    client.post("/api/plugin/import", json=payload, follow_redirects=False)

    with sqlite3.connect("athenaeum.db") as c:
        got = dict(c.execute("SELECT datetime, type FROM annotations").fetchall())
    assert got == {
        "2024-01-01T09:00:00": "bookmark",
        "2024-01-02T09:00:00": "bookmark",
        "2024-01-03T09:00:00": "highlight",
        "2024-01-04T09:00:00": "note",
        "2024-01-05T09:00:00": "bookmark",
        "2024-01-06T09:00:00": "highlight",
    }, "KOReader classifies by drawer/colour; bookmark text is the chapter label"


def test_manual_annotation_types_follow_content(client):
    client.post("/books", data={"title": "Manual"}, follow_redirects=False)
    cases = [
        ({"text": "only a quote", "pageno": "1"}, "highlight"),
        ({"note": "only a note", "pageno": "2"}, "note"),
        ({"text": "quote", "note": "note", "pageno": "3"}, "note"),
        ({"pageno": "4"}, "bookmark"),
    ]
    for data, _ in cases:
        client.post("/books/1/annotations/edit", data=data, follow_redirects=False)

    with sqlite3.connect("athenaeum.db") as c:
        got = [r[0] for r in c.execute("SELECT type FROM annotations ORDER BY pageno")]
    assert got == [expected for _, expected in cases], "the form has no position, so content decides"


def test_editing_a_bookmark_keeps_its_type_and_label(client):
    md5 = "b" * 32
    payload = {
        "version": "0.3.0",
        "books": [{"title": "Moby Dick", "authors": "Melville", "pages": 720, "md5": md5}],
        "stats": [],
        "annotations": {md5: [
            {"datetime": "2024-01-07T09:00:00", "page": 100, "pageno": 100,
             "chapter": "Loomings", "text": "in Loomings", "note": "first"},
        ]},
    }
    client.post("/api/plugin/import", json=payload)

    form = client.get(
        "/books/1/annotations/edit",
        params={"datetime": "2024-01-07T09:00:00", "page_ref": "100"},
    ).text
    assert "Edit bookmark" in form
    assert 'name="text"' not in form, "a bookmark's chapter label is not editable"
    assert "first" in form

    client.post(
        "/books/1/annotations/edit",
        data={"datetime": "2024-01-07T09:00:00", "page_ref": "100",
              "note": "updated", "pageno": "100", "chapter": "Loomings"},
        follow_redirects=False,
    )

    with sqlite3.connect("athenaeum.db") as c:
        row = c.execute("SELECT type, text, note FROM annotations").fetchone()
    assert row == ("bookmark", "in Loomings", "updated")

    page = client.get("/books/1").text
    assert "No bookmarks yet" not in page and "updated" in page
    assert "No highlights yet" in page, "a bookmark must stay out of the highlights section"


def test_book_page_shows_empty_annotation_sections(client):
    client.post("/books", data={"title": "Nothing yet"}, follow_redirects=False)
    page = client.get("/books/1").text
    assert "No highlights yet" in page
    assert "No bookmarks yet" in page
    assert "Add annotation" in client.get("/books/1/annotations/edit").text

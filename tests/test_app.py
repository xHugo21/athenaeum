import os
import sqlite3

from app.koreader import parse_koreader
from app.main import app  # ensures import works
from fastapi.testclient import TestClient


def koreader_bytes(with_duplicate=False) -> bytes:
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE book (
          id INTEGER PRIMARY KEY, title TEXT, authors TEXT, pages INTEGER,
          total_read_time INTEGER, total_read_pages INTEGER, last_open INTEGER, md5 TEXT);
        CREATE TABLE page_stat (id_book INTEGER, page INTEGER, start_time INTEGER, duration INTEGER);
        CREATE TABLE page_stat_data (id_book INTEGER, page INTEGER, start_time INTEGER, duration INTEGER, total_pages INTEGER DEFAULT 0);
        INSERT INTO book VALUES (1, '  The   Hobbit ', 'J.R.R. Tolkien', 300, 180, 2, 1700086400, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        INSERT INTO page_stat VALUES (1, 1, 1700000000, 60);
        INSERT INTO page_stat VALUES (1, 2, 1700086400, 120);
        INSERT INTO page_stat_data VALUES (1, 1, 1700000000, 60, 300);
        INSERT INTO page_stat_data VALUES (1, 2, 1700086400, 120, 300);
        """
    )
    if with_duplicate:
        con.execute("INSERT INTO book VALUES (2, 'The Hobbit', 'J. r. r. Tolkien', 300, 30, 1, 1700172800, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')")
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


def test_import_and_reimport(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = koreader_bytes(with_duplicate=True)

    with TestClient(app) as client:
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


def test_file_reimport_propagates_title_on_md5_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    md5 = "a" * 32

    def koreader_with(title: str) -> bytes:
        con = sqlite3.connect(":memory:")
        con.executescript(
            f"""
            CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT, authors TEXT, pages INTEGER,
                total_read_time INTEGER, total_read_pages INTEGER, last_open INTEGER, md5 TEXT);
            CREATE TABLE page_stat_data (id_book INTEGER, page INTEGER, start_time INTEGER, duration INTEGER, total_pages INTEGER DEFAULT 0);
            INSERT INTO book VALUES (1, '{title}', 'J.R.R. Tolkien', 300, 180, 2, 1700086400, '{md5}');
            INSERT INTO page_stat_data VALUES (1, 1, 1700000000, 60, 300);
            """
        )
        return bytes(con.serialize())

    with TestClient(app) as client:
        r = client.post("/import", files={"file": ("statistics.sqlite3", koreader_with("  The  Hobbit  "), "application/octet-stream")}, follow_redirects=False)
        assert r.status_code == 303

        r = client.post("/import", files={"file": ("statistics.sqlite3", koreader_with("The Hobbit, or There and Back Again"), "application/octet-stream")}, follow_redirects=False)
        assert r.status_code == 303
        assert "imported=0" in r.headers["location"], "same md5 = same book, no new row"

    with sqlite3.connect("athenaeum.db") as c:
        (n,) = c.execute("SELECT COUNT(*) FROM books").fetchone()
        assert n == 1
        (title,) = c.execute("SELECT title FROM books").fetchone()
        assert title == "The Hobbit, or There and Back Again", "md5 match must propagate corrected title"


def test_manual_add_with_isbn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from app import main

    class FakeResp:
        status_code = 200

        def json(self):
            return {"docs": [{"title": "Matilda", "author_name": ["Roald Dahl"]}]}

    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: FakeResp())
    with TestClient(app) as client:
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


def test_set_isbn_fills_pages_for_manual_add(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from app import main

    class FakeResp:
        status_code = 200

        def json(self):
            return {"docs": [{"title": "Matilda", "author_name": ["Roald Dahl"], "number_of_pages_median": 96}]}

    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: FakeResp())
    with TestClient(app) as client:
        client.post("/books", data={"title": "Manual Book", "pages": ""}, follow_redirects=False)
        r = client.post("/books/1/edit", data={"title": "Manual Book", "isbn": "9780140328721"}, follow_redirects=False)
        assert r.status_code == 303
    with sqlite3.connect("athenaeum.db") as c:
        (total, read) = c.execute("SELECT total_pages, pages_read FROM books WHERE id=1").fetchone()
    assert (total, read) == (96, 96), "metadata pages must fill NULL total_pages even for manual adds"


def test_stats_page(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        client.post("/import", files={"file": ("statistics.sqlite3", koreader_bytes(), "application/octet-stream")})
        r = client.get("/stats")
        assert r.status_code == 200
        assert "The Hobbit" in r.text
        assert "3m" in r.text


def test_reject_garbage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        r = client.post("/import", files={"file": ("x.sqlite3", b"not a db at all", "application/octet-stream")}, follow_redirects=False)
        assert r.status_code == 303
        assert "error" in r.headers["location"]


def test_plugin_sync_no_dupes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
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
            {"datetime": "2024-01-07T09:00:00", "page": 100},
        ]},
        "device_id": "dev1",
    }
    with TestClient(app) as client:
        r = client.post("/api/plugin/device", json={"id": "d1", "model": "kobo"}, follow_redirects=False)
        assert r.status_code == 200, "device endpoint must exist for the plugin"

        r = client.post("/api/plugin/import", json=payload, follow_redirects=False)
        assert r.status_code == 200 and r.json()["created"] == 1

        r = client.post("/api/plugin/import", json=payload, follow_redirects=False)
        assert r.json()["created"] == 0, "re-sync must not duplicate"

        r = client.post("/api/plugin/import", json=dict(payload, version="9.9"), follow_redirects=False)
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

    with TestClient(app) as client:
        r = client.get("/books/1")
        assert "Call me Ishmael" in r.text


def test_old_db_migration_with_nofcover_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    con = sqlite3.connect("athenaeum.db")
    con.executescript(
        """
        CREATE TABLE books (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT, isbn TEXT, md5 TEXT,
            total_seconds INTEGER NOT NULL DEFAULT 0, pages_read INTEGER NOT NULL DEFAULT 0,
            total_pages INTEGER, last_read_at INTEGER, rating REAL, review TEXT,
            added_at INTEGER NOT NULL);
        INSERT INTO books (title, isbn, added_at) VALUES ('Old Book', '9780140328721', 0);
        """
    )
    con.commit()
    con.close()
    os.makedirs("covers")
    open("covers/.nofCover", "a").close()

    from app import main

    with TestClient(main.app) as client:
        cols = {r[1] for r in sqlite3.connect("athenaeum.db").execute("PRAGMA table_info(books)")}
        assert "cover_failed" in cols, "migration must add cover_failed to existing DBs"
        assert not os.path.exists("covers/.nofCover"), "global flag file must be removed"
        (failed,) = sqlite3.connect("athenaeum.db").execute("SELECT cover_failed FROM books WHERE id=1").fetchone()
        assert failed == 0

        class FakeResp:
            status_code = 200
            content = b"x" * 2000

        monkeypatch.setattr(main.httpx, "get", lambda *a, **k: FakeResp())
        r = client.get("/covers/1.jpg")
        assert r.status_code == 200, "old book must still get its cover fetched"


def test_highlights_export(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
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
            (1, "2024-01-02 09:00:00", "42", "bookmark", None, None, None, 42, 200, None),
        )
        con.commit()
        con.close()
        r = client.get("/books/1/highlights.md")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/markdown")
        assert 'filename="1984-highlights.md"' in r.headers["content-disposition"]
        body = r.text
        assert "# 1984" in body and "*Orwell*" in body
        assert "> It was a bright cold day in April." in body
        assert "*— Chapter 1 — p. 5*" in body
        assert "> The clocks were striking thirteen." in body
        assert "**Note:** Surreal." in body
        assert "Bookmarks" not in body
        assert "p. 42" not in body

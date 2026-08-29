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
          total_read_time INTEGER, total_read_pages INTEGER, last_open INTEGER);
        CREATE TABLE page_stat (id_book INTEGER, page INTEGER, start_time INTEGER, duration INTEGER);
        INSERT INTO book VALUES (1, '  The   Hobbit ', 'J.R.R. Tolkien', 300, 180, 2, 1700086400);
        INSERT INTO page_stat VALUES (1, 1, 1700000000, 60);
        INSERT INTO page_stat VALUES (1, 2, 1700086400, 120);
        """
    )
    if with_duplicate:
        con.execute("INSERT INTO book VALUES (2, 'The Hobbit', 'J. r. r. Tolkien', 300, 30, 1, 1700172800)")
        con.execute("INSERT INTO page_stat VALUES (2, 1, 1700172800, 30)")
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
    assert sum(s.months.values()) == 210
    assert parse_koreader(koreader_bytes())[0].total_seconds == 180


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

    import sqlite3

    with sqlite3.connect("athenaeum.db") as c:
        (n,) = c.execute("SELECT COUNT(*) FROM books").fetchone()
        assert n == 1, "re-import must not duplicate"
        (title, author, tot, pages) = c.execute(
            "SELECT title, author, total_seconds, pages_read FROM books"
        ).fetchone()
        assert (title, author, tot, pages) == ("The Hobbit", "J.R.R. Tolkien", 210, 3)
        ms = c.execute("SELECT month, seconds FROM month_seconds ORDER BY month").fetchall()
        assert sum(s for _, s in ms) == 210
        (rating, review) = c.execute("SELECT rating, review FROM books").fetchone()
        assert (rating, review) == (5, "nice"), "re-import must keep rating/review"


def test_manual_add_with_isbn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import sqlite3

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
    with sqlite3.connect("athenaeum.db") as c:
        row = c.execute("SELECT title, author, isbn, rating FROM books").fetchone()
    assert row == ("Matilda", "Roald Dahl", "9780140328721", 5)


def test_reject_garbage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        r = client.post("/import", files={"file": ("x.sqlite3", b"not a db at all", "application/octet-stream")}, follow_redirects=False)
        assert r.status_code == 303
        assert "error" in r.headers["location"]

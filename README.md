<div align="center">
    <br/>
    <p>
        <img width="120" title="Athenaeum" alt="athenaeum logo" src="assets/logo.png" />
        <h1>athenaeum</h1>
    </p>
    <p width="120">
        Personal reading archive: KOReader statistics, highlights and your own verdicts in one private place.
    </p>
    <br/>
</div>

<div align="center">
  <a href="assets/demo.mp4"><img width="70%" alt="Walkthrough of athenaeum: filtering the library by label, the shelf view, a book with reading speed and time left, its highlights, the to-read list, label management, reading statistics and the calendar" src="assets/demo.gif" /></a>
  <img width="1.5%" alt="" src="assets/divider.svg" />
  <a href="assets/demo-mobile.mp4"><img width="21%" alt="The library, a book with reading speed and time left, its highlights, the to-read list and the stats page at phone width, in dark mode" src="assets/demo-mobile.gif" /></a>
</div>

## Features

- **Library** — cover grid or shelf view with search, sorting (title, author, rating, time, pages, added) and filters by status, rating and label.
- **Command palette** — `⌘K` / `Ctrl+K` from any page to jump to a book, a section or an action: add a book, toggle the theme, download the database, sign out.
- **Books** — title, author, ISBN, pages, reading time, progress, rating, review and cover. Add books manually (metadata filled in from Open Library by ISBN), edit, rate or delete them, or select several at once for bulk labeling and status changes.
- **Pace & time left** — reading speed in pages per hour, estimated time left in the current book and a projected finish date based on your last two weeks.
- **KOReader import** — sync books, sessions, highlights, notes and bookmarks over Wi-Fi with koinsight.koplugin, or upload the device's SQLite file.
- **Annotations** — highlights, notes and bookmarks per book, imported from KOReader or added by hand, editable and deletable, exportable as Markdown.
- **Share story** — turn a book's cover, rating and a highlight you pick into a 1080×1920 image, shared from the phone or downloaded as a file. Rendered in the browser, nothing leaves the server.
- **Statistics** — totals, current and best streak, best and longest day, 12-month reading, average time per weekday, time-of-day profile and top books, all time or any single year.
- **Calendar** — per-day reading time and pages, broken down by book.
- **To-read list** — wishlist kept separate from the library.
- **Labels** — tag books and browse the library by tag.
- **Covers** — fetched from Open Library by ISBN, or uploaded manually; a generated placeholder when offline.
- **Own your data** — whole SQLite database downloadable at any time; single file, no external service.
- **Private by default** — password gate, single-owner instance, dark mode, phone and desktop layouts.

## Run locally

```sh
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port <port>
```

Accessible through http://localhost:<port>

Pages read only counts books with a print page or ISBN page count. Print page counts are fetched from Open Library when a book is added or its ISBN is edited. For books already in the database, set **Print pages** from the book's edit form; a manual value overrides the ISBN one, and clearing it falls back to the ISBN value when there is one.

## Docker

```sh
docker build -t athenaeum .
docker run -p 8000:8000 -v athenaeum-data:/data athenaeum
```

## Docker Compose

```yaml
services:
  athenaeum:
    image: ghcr.io/xhugo21/athenaeum:latest
    user: "568:568"
    ports:
      - "9372:8000"
    volumes:
      - /stacks/athenaeum:/data
    restart: unless-stopped
    environment:
      - ATHENAEUM_PASSWORD=changeme
```

## KOReader plugin sync

Supports [koinsight.koplugin](https://github.com/Ko-Insight/KoInsight/tree/master/plugins/koinsight.koplugin) (kudos to KoInsight).

Syncs books, reading stats, highlights, notes and bookmarks straight from the device over Wi-Fi.

Install on your e-reader and point it to your athenaeum URL

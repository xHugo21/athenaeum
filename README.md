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
<img width="90%" alt="Walkthrough of athenaeum: searching the library, opening a book, browsing the shelf view, reading the stats page, and switching to dark mode" src="assets/demo.gif" />
<p><sub>20-second tour &middot; <a href="assets/demo.mp4">full-quality MP4 (1.7&nbsp;MB)</a></sub></p>
</div>

## Run locally

```sh
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port <port>
```

Accessible through http://localhost:<port>

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

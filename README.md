# athenaeum

Personal book tracking web app.

Allows manual entries and automatic import of KOReader statistics and highlights.

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
```

## KOReader plugin sync

Supports [koinsight.koplugin](https://github.com/Ko-Insight/KoInsight/tree/master/plugins/koinsight.koplugin) (kudos to KoInsight).

Syncs books, reading stats, highlights, notes and bookmarks straight from the device over Wi-Fi.

Install on your e-reader and point it to your athenaeum URL

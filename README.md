# athenaeum

Personal book tracking web app.

Allows import of KOReader statistics and manual entries.

## Run

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

## KOReader plugin sync

The [koinsight.koplugin](https://github.com/Ko-Insight/KoInsight/tree/master/plugins/koinsight.koplugin)
plugin syncs books, reading stats, highlights, notes and bookmarks straight
from the device over Wi-Fi.

Install it on your ereader and point the url to your athenaeum URL

# athenaeum

Personal book tracking web app.

Allows import of KOReader statistics and manual entries.

## Run

```sh
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Accessible through http://localhost:8000

## Docker

```sh
docker build -t athenaeum .
docker run -p 8000:8000 -v athenaeum-data:/data athenaeum
```

## KOReader plugin sync

The [koinsight.koplugin](https://github.com/Ko-Insight/KoInsight/tree/master/plugins/koinsight.koplugin)
plugin syncs books, reading stats, highlights, notes and bookmarks straight
from the device over Wi-Fi are rebuilt from raw sessions on every sync.

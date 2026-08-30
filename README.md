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

## KOReader plugin sync (highlights & bookmarks)

The [koinsight.koplugin](https://github.com/Ko-Insight/KoInsight/tree/master/plugins/koinsight.koplugin)
plugin syncs books, highlights, notes and bookmarks straight from the device.
It does **not** send the statistics.sqlite3 file, so plugin sync only populates
books and annotations — reading-time stats come from the manual
statistics.sqlite3 upload in the frontend, which fills in the same books (both
paths match books by md5 first, then title/author, so nothing is duplicated).

1. Copy the `koinsight.koplugin` folder into `koreader/plugins/` on the e-reader.
2. In KOReader: Tools → KoInsight → Set server URL → `http://<server-LAN-IP>:8000`
   (e.g. `http://192.168.1.36:8000`).
3. Tools → KoInsight → Synchronize data.

The plugin payload includes per-page reading rows, but athenaeum ignores them
deliberately: importing them alongside manual statistics.sqlite3 uploads would
double-count. Syncing is idempotent — re-syncs update in place.

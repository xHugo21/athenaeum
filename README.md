# athenaeum

Personal book tracking web app.

Allows manual entries and automatic import of KOReader statistics and highlights.
<div align="center">
<img width="80%" alt="screenshot1" src="https://github.com/user-attachments/assets/e107e845-0bab-426c-af97-9541952be6ba" />
<br /><br />
<img width="80%" alt="screenshot2" src="https://github.com/user-attachments/assets/29bb7186-5fed-471f-9a5c-39e75207bdbb" />
<br /><br />
<img width="80%" alt="screenshot3" src="https://github.com/user-attachments/assets/1ed5e86d-6eea-40ed-98b9-7b2050473bc5" />
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

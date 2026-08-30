# athenaeum

Personal book tracking web app.

Allows import of KOReader statistics and manual entries.

## Run

```sh
uv sync
uv run uvicorn app.main:app --port 8000 --reload
```

Open http://localhost:8000.

## Docker

```sh
docker build -t athenaeum .
docker run -p 8000:8000 -v athenaeum-data:/data athenaeum
```

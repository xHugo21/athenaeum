# Forking koinsight.koplugin

Status: proposal. Nothing here is implemented on the device yet.

Upstream: <https://github.com/Ko-Insight/KoInsight/tree/master/plugins/koinsight.koplugin> (MIT).
Deployed version: `0.3.0` (`const.lua`), which is also the server's minimum.

## Why fork

Three things need to happen on the device side that upstream has no reason to do for one
personal server:

1. **Annotations must carry their `type`.** The server currently infers it with
   `plugin_ann_type`, and that inference is a guess (see below).
2. **The plugin API must authenticate.** `/api/plugin/device` and `/api/plugin/import` are
   exempt from `ATHENAEUM_PASSWORD` in `app/main.py`'s auth middleware, so anyone on the
   network can write to the database.
3. **Malformed payloads must fail as 4xx, not 500.** The server parses the version with
   `int(v[0])` and reads JSON before validating it.

The fork stays small on purpose: a handful of lines in four files. Everything else keeps
tracking upstream.

## Why `type` cannot be inferred from content

KOReader stores a page bookmark's chapter label in `text`:

```lua
-- koreader/frontend/apps/reader/modules/readerbookmark.lua, toggleBookmark
text = T(_("in %1"), chapter)
```

and classifies annotations by `drawer`, ignoring `text`:

```lua
function ReaderBookmark.getBookmarkType(bookmark)
  if bookmark.drawer then
    if bookmark.note then return "note" end
    return "highlight"
  end
  return "bookmark"
end
```

The plugin forwards `text` verbatim (`annotation_reader.lua`, `cleanAnnotations`), so a
plain bookmark in a book with a TOC arrives as `text = "in Loomings"`. A content rule
("bookmark when there is no text or note") would turn every one of those into a highlight
with the chapter title as its quote. That is why `app/main.py` keeps two rules:

- `plugin_ann_type` — position metadata decides; mirrors `getBookmarkType`.
- `manual_ann_type` — content decides; the web form has no position to send.

## Change 1 — send `type` with every annotation

File: `annotation_reader.lua`, function `cleanAnnotations`. Add one field:

```lua
local cleaned_annotation = {
  datetime = annotation.datetime,
  drawer = annotation.drawer,
  color = annotation.color,
  text = annotation.text,
  note = annotation.note,
  type = annotation.drawer and (annotation.note and "note" or "highlight") or "bookmark",
  chapter = annotation.chapter,
  pageno = annotation.pageno,
  page = annotation.page,
  total_pages = total_pages,
}
```

Both annotation paths go through `cleanAnnotations` (`getAnnotationsByBook` for the current
book, `send_book_annotations` for the bulk sync), so one edit covers both.

Server side: none. `insert_annotation` already prefers `a["type"]` and keeps
`plugin_ann_type` as the fallback for 0.3.x clients.

Verification after syncing:

```sh
sqlite3 athenaeum.db "select type, count(*) from annotations group by type"
```

A bookmark must stay `bookmark` even though its `text` is `in <chapter>`, and a bookmark
with a note must stay `bookmark` too.

## Change 2 — authenticate the plugin API

Plugin files: `upload.lua` (`get_headers`, line ~17), `settings.lua` (new setting).

```lua
function get_headers(body)
  local headers = {
    ["Content-Type"] = "application/json",
    ["Content-Length"] = tostring(#body),
  }
  local token = G_reader_settings:readSetting("koinsight_token")
  if token then
    headers["Authorization"] = "Bearer " .. token
  end
  return headers
end
```

Add a token field to the config dialog next to the server URL, and store it with
`G_reader_settings:saveSetting("koinsight_token", token)`.

Server: require the token on `/api/plugin/*` when `ATHENAEUM_PASSWORD` is set, keep
`/login` and `/static/*` public, and expose the token in the UI so it can be copied or
rotated. The token is a bearer secret stored in KOReader's settings; plain HTTP over a
home LAN is the current transport.

**Rollout order matters:** install the fork and configure the token *before* enforcing it
server-side. Enforcing first breaks every sync from a 0.3.x device.

## Change 3 — version and payload validation

- Bump `const.VERSION` to `0.4.0`. The server's floor stays `>=0.3.0`; raise it only after
  every device has the fork.
- Keep the plugin version in the payload. Optionally add an integer `schema = 2` so a
  future payload change does not need a new plugin version.
- Server: parse the version with a regex or `try/except` and return 400; validate the body
  shape before touching the database. Today a `bad` version string is handled, but a
  non-numeric one can raise and return 500.

## Change 4 (optional) — device identity

`/api/plugin/device` is a deliberate stub. The fork already sends
`{id, model, version}`; a real registry could dedupe imports per device and allow
revoking one device's token. Not needed with a single reader.

## What we do not touch

- Stats payload, `db_reader.lua`, the upload flow and the endpoint paths stay upstream.
- Do not rename `/api/plugin/import`; the server depends on it.
- The `statistics.sqlite` file import is a separate path in `app/koreader.py` and is not
  part of this fork.

## Compatibility

| Plugin | Server | Result |
| --- | --- | --- |
| 0.3.x | today | works (server infers type) |
| 0.4.0 | today | works, explicit `type` wins |
| 0.3.x | token enforced | sync blocked |
| 0.4.0 | token enforced | works |

No coordinated release: the current server already reads `type`, and old servers ignore
unknown JSON fields.

## Maintenance

Watch upstream for changes to:

- `annotation_reader.lua` — the `cleanAnnotations` field list (where `type` goes).
- `upload.lua` — headers and the payload.
- `settings.lua` — the config dialog.
- `const.lua` — the version.

Keep the diff under ~30 lines and record the upstream commit SHA the fork is based on in
its own README. If upstream ever sends `type` itself, drop Change 1 and keep the rest.

## Rollout checklist

1. Build a zip of `koinsight.koplugin/` and copy it into `koreader/plugins/`.
2. Sync and check `type` counts as above.
3. Configure the token on the device and sync again.
4. Enable token enforcement on the server.
5. Keep the 0.3.0 zip for rollback.

## Non-goals

- Publishing the fork to a plugin registry.
- Changing KoInsight's own web app.
- Merging the manual and plugin type rules in the server: the payloads differ, and
  position is the only reliable signal for the plugin.

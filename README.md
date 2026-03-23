# Google in a Day — Web Crawler + Search Engine

A web crawler and search engine written in Python. Crawl any website to a configurable depth, build a word-frequency index, and search it in real time — even while the crawl is still running. Core logic uses only Python's standard library; Flask handles the web layer.

---

## What it does

- **Crawls** any website recursively up to a configurable link depth, following only same-domain links
- **Indexes** every page as a word-frequency map stored on disk, sharded by first letter
- **Searches** the live index in real time — results appear before the crawl finishes
- **Monitors** progress on a live dashboard: queue depth, URLs/sec, back-pressure indicator, and a scrolling log

---

## Install and run

```bash
pip install flask
python server.py
```

Then open **http://localhost:3600** in your browser.

No other dependencies. All crawling and HTML parsing uses Python's standard library only.

---

## The three pages

| Page | URL | Purpose |
|------|-----|---------|
| **New Crawl** | `/` | Enter an origin URL and depth, start a crawl job |
| **Crawler Status** | `/status/<crawler_id>` | Live stats, queue depth, back-pressure indicator, and scrolling log |
| **Search** | `/search` | Query the index with paginated, ranked results |

After submitting a crawl job, the browser redirects automatically to the Status page for that job. Search works at any point — even while a crawl is in progress.

---

## Architecture

```
server.py          Flask API + HTML routes
  │
  ├── crawler.py   Core crawl engine (threading, urllib, html.parser)
  └── searcher.py  Search + ranking + pagination

data/storage/      Word index shards  (a.data, b.data, …)
data/              Crawl job state    (<epochtime_uuid8>.data)
```

### `crawler.py` — Core engine

`CrawlerJob` manages the full lifecycle of a single crawl. Calling `job.start()` returns a `crawler_id` immediately and launches the crawl in a background thread. The worker loop dequeues URLs, fetches each page with `urllib.request`, parses links and word frequencies with `html.parser`, writes the index to disk, and saves a state snapshot after every page.

### `searcher.py` — Search engine

`search(query, index_store, index_lock)` tokenises the query, looks up each word across both the live in-memory index and the on-disk shards, scores every matching URL, and returns a ranked list. `paginate()` slices the result list for the UI.

### `server.py` — Flask API

Thin Flask layer. Wires a single shared `index_store` dict (and its `RLock`) into every `CrawlerJob` so all jobs feed one searchable index. The status page uses two independent polling loops: stat cards hit `/api/crawl/<id>/status` every second, and log entries stream via a long-poll on `/api/status/poll/<id>` that holds for up to 20 seconds.

---

## Key design decisions

**Thread-safe `RLock` on all shared state**
Both `visited_urls` (set) and `index_store` (dict) are accessed by multiple crawler threads and the Flask request threads simultaneously. Every read and write acquires the corresponding `threading.RLock()`. Lock scope is kept minimal — data is copied out of the lock before any I/O or computation.

**`queue.Queue(maxsize=500)` for back pressure**
The URL work queue has a hard size cap. When it fills up, `put_nowait()` raises `queue.Full` and the URL is dropped rather than buffered. This keeps memory use bounded on large or densely-linked sites. The status dashboard shows `back_pressure_active` when drops are happening.

**Native `urllib` only — no third-party HTTP libraries**
All HTTP fetches use `urllib.request.urlopen` with a custom `User-Agent` header and a 10-second timeout. URL normalisation and resolution use `urllib.parse`. No `requests`, `httpx`, or `aiohttp`.

**Word index sharded by first letter**
The on-disk index is split into 26 files (`data/storage/a.data` … `data/storage/z.data`). Each shard is plain text, one line per entry: `word url origin_url depth frequency`. A search query only reads the shards for letters that appear in the query tokens, keeping I/O proportional to query length rather than index size. Shards are written atomically via a `.tmp` + `os.replace()` pattern so the server never reads a partially-written file.

**Search scope**
Search runs across all indexed data from every crawl job in the current session, plus anything written to the on-disk shards from previous sessions. It is not scoped to a single crawl job.

**Search scoring**
`relevance_score = (frequency × 10) + 1000 - (depth × 5)`. Title tokens are weighted 3× during indexing in `crawler.py`. Results are keyed by `(relevant_url, origin_url, depth)` so the same URL discovered via different origin/depth combinations is returned as separate entries.

**Resume**
Partial persistence — visited URLs are saved per job; the queue is not persisted. This avoids re-crawling already-visited pages if the same job ID is reused, but is not a full resume.

---

## Known Limitations
- Single worker thread per job; rate limit is per-job, not global
- Back pressure drops URLs (lossy); it does not block the producer
- Resume is partial: visited URLs are persisted, but the queue is not
- URL canonicalization is intentionally minimal: www-prefix and trailing-slash normalization only

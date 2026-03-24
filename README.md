# Google in a Day — Web Crawler + Search Engine

**Repository:** [https://github.com/ozlemlcetin/google-crawler](https://github.com/ozlemlcetin/google-crawler)

A web crawler and search engine written in Python. Crawl any website to a configurable depth, build a word-frequency index, and search it in real time — even while the crawl is still running. Core logic uses only Python's standard library; Flask handles the web layer.

---

## Install and run

```bash
pip install flask
python server.py
```

Open **http://localhost:3600** in your browser.

No other dependencies. All crawling and HTML parsing uses Python's standard library only (`urllib`, `html.parser`, `threading`, `queue`).

---

## The three pages

| Page | URL | Purpose |
|------|-----|---------|
| **New Crawl** | `/` | Enter an origin URL and depth, start a crawl job |
| **Crawler Status** | `/status/<crawler_id>` | Live stats, queue depth, back-pressure indicator, scrolling log |
| **Search** | `/search` | Query the index with paginated, ranked results |

After submitting a crawl job the browser redirects to the Status page. Search works at any time — even while a crawl is in progress.

---

## Architecture

```
server.py          Flask API + HTML routes
  │
  ├── crawler.py   Core crawl engine (threading, urllib, html.parser)
  └── searcher.py  Search + ranking + pagination

data/storage/      Word index shards  (one file per first alphabetic character)
data/              Crawl job state    (<epochtime_uuid8>.data)
                   Visited-URL snapshot  (<id>_visited.data)
                   Pending-URL frontier  (<id>_frontier.data)
```

### `crawler.py` — Core engine

`CrawlerJob` manages the full lifecycle of one crawl. `job.start()` returns a `crawler_id` immediately and launches the crawl in a background daemon thread. The worker loop dequeues URLs, fetches each page with `urllib.request`, parses links and word frequencies with `html.parser`, writes the index to disk, and saves three artifacts after every page: a job-state snapshot, a visited-URL snapshot, and a pending-frontier snapshot (for resume support).

### `searcher.py` — Search engine

`search(query, index_store, index_lock)` tokenises the query, looks up each word across both the live in-memory index and the on-disk shards, merges by `(url, origin_url, depth)` triple, scores each entry, and returns a ranked list. `paginate()` slices the result list for the UI.

### `server.py` — Flask API

Thin Flask layer. Wires a single shared `index_store` dict (and its `RLock`) into every `CrawlerJob` so all jobs feed one searchable index. The status page uses two independent polling loops: stat cards call `/api/crawl/<id>/status` every second (returns live in-memory state when available, falls back to last disk snapshot), and log entries stream via a long-poll on `/api/status/poll/<id>` that holds for up to 20 seconds.

---

## Key design decisions

### Thread-safe `RLock` on all shared state

`visited_urls`, `enqueued_urls`, and `_frontier_items` share one `_visited_lock` (RLock). `index_store` has its own `_index_lock`. `status` fields share `_status_lock`. Every read/write of these structures acquires the relevant lock.

`_save_index()` snapshots the index data under `_index_lock`, then releases the lock before writing to disk. This keeps the critical section short — disk I/O is not under the lock.

`_save_state()` and `_save_frontier()` call `get_state()` or snapshot under lock, then do their I/O after releasing.

### Bounded URL queue with configurable back pressure

The URL work queue has a hard size cap (default 500, configurable via `max_queue_size` 10–5000). When it fills up, `put_nowait()` raises `queue.Full` and the URL is dropped rather than buffered. This keeps memory use bounded on densely-linked sites. The status dashboard shows `back_pressure_active` when drops are occurring.

### Native `urllib` only — no third-party HTTP libraries

All HTTP fetches use `urllib.request.urlopen` with a custom `User-Agent` header and a 10-second timeout. URL normalisation and resolution use `urllib.parse`. No `requests`, `httpx`, or `aiohttp`.

### Word index sharded by first letter

The on-disk index is split into one file per first alphabetic character of each indexed token (e.g. `data/storage/a.data`, `data/storage/é.data`). Because Python's `isalpha()` matches any Unicode letter, crawling non-ASCII content produces more than 26 shards. Each shard is plain text, one line per entry: `word url origin_url depth frequency`. A search query only reads the shards for letters that appear in the query tokens. Shards are written atomically via a `.tmp` + `os.replace()` pattern so the server never reads a partially-written file.

### Duplicate-crawl dedup in the index

`_update_index()` checks whether the exact `(url, origin_url, depth)` triple is already present before appending. If the same page is indexed twice (e.g., from two separate crawl jobs in the same server session), the second occurrence is silently dropped. This prevents score inflation from repeated crawls without changing search behavior for legitimately distinct entries.

### Search scoring

`relevance_score = (frequency × 10) + 1000 - (depth × 5)`. Title tokens are weighted 3× during indexing. Results are keyed by `(relevant_url, origin_url, depth)` so the same URL discovered via different crawl paths appears as separate entries.

### Search scope

Search runs across all indexed data from every crawl job in the current server session, plus anything written to the on-disk shards from previous sessions. It is not scoped to a single crawl job.

### Visited-URL guarantee

A URL is **not crawled twice within the same crawl job**. `visited_urls` is a per-job set, populated at dequeue time and checked before every fetch. `enqueued_urls` is a parallel guard that prevents a URL from entering the queue twice in the same job. Both sets are in memory only; they are not shared across jobs. Two different crawl jobs can therefore crawl the same URL independently.

---

## Persistence

Three types of files are written to `data/` per crawl job:

| File | Format | Purpose |
|------|--------|---------|
| `<id>.data` | JSON | Job state snapshot: status, URL counts, logs. Written after every page. |
| `<id>_visited.data` | Plain text (one URL per line) | Visited-URL snapshot. Written after every page. Used by the resume feature. |
| `<id>_frontier.data` | JSON dict | Pending-URL frontier: `url → {origin_url, depth}`. Written after every page. Used by the resume feature. |

The shared word index is stored in `data/storage/{letter}.data` (plain text, one entry per line). These shards accumulate across all crawl jobs and server sessions. They are **not** cleared on restart.

**Note:** The `data/` directory may contain job state files (`*.data`, `*_visited.data`, `*_frontier.data`) and index shards (`storage/*.data`) from previous runs. They are safe to leave in place or delete for a clean start:

```bash
rm -f data/*.data data/storage/*.data
```

---

## Resume feature

An interrupted crawl (stopped by the user, or killed by process exit) can be resumed from its saved frontier and visited-URL snapshot.

### How resume works

1. After every page, the crawler writes `<id>_frontier.data` (pending URLs with their origin/depth) and `<id>_visited.data` (all visited URLs).
2. `POST /api/crawl/<id>/resume` loads these files, creates a new `CrawlerJob` pre-seeded with the frontier and pre-populated with the visited set, and starts it.
3. The new job skips already-visited URLs and continues from the saved frontier — it does not restart from the origin.
4. A new `crawler_id` is returned. The original job's data files are preserved.

### Resume is not available if

- The job completed normally (`status: done`) — nothing to resume.
- The job is still running in the current server process.

### Manual test checklist for resume

```
1. Open http://localhost:3600, start a crawl of a small site (depth 2).
2. Watch the Status page until a few pages have been processed.
3. Stop the server with Ctrl-C (or click the Stop button, then kill the server).
4. Restart: python server.py
5. Resume via the API:
      curl -X POST http://localhost:3600/api/crawl/<original_id>/resume
   Response: {"crawler_id": "<new_id>", "resumed_from": "<original_id>"}
6. Open http://localhost:3600/status/<new_id>
7. Confirm the log shows "Resuming crawl" with a non-zero skipping_visited count.
8. Confirm crawling continues (URLs processed count rises) without restarting from zero.
9. Search http://localhost:3600/search?q=<word> at any time — results should appear.
```

---

## API reference

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/crawl` | Start a new crawl. Body: `{origin_url, max_depth, rate_limit?, max_queue_size?}` |
| `GET` | `/api/crawl/<id>/status` | Current job state (in-memory if live, disk snapshot otherwise) |
| `POST` | `/api/crawl/<id>/stop` | Signal the worker to stop after the current page |
| `POST` | `/api/crawl/<id>/resume` | Resume an interrupted crawl from saved frontier |
| `GET` | `/api/crawl/list` | List known jobs (in-memory first, then disk) |
| `GET` | `/api/search?q=<query>` | Search the index. Optional: `page`, `per_page` |
| `GET` | `/api/status/poll/<id>?since=N` | Long-poll for new log entries (holds ≤20 s) |

---

## Known limitations

- Single worker thread per job; rate limit is per-job, not global
- Back-pressure is lossy: when the queue is full, discovered URLs are dropped (not retried)
- Resume frontier is limited to `max_queue_size` entries on reload; if more URLs were pending they are dropped (back-pressure applies equally to resume seeding)
- URL canonicalisation is minimal: www-prefix and trailing-slash normalisation only; query-string dedup is not performed
- JavaScript-rendered content is not supported (static HTML only)
- Same-host scoping only: the crawler follows links within the origin domain; cross-domain links are counted and logged but not followed

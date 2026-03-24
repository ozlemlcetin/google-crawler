# Product Requirement Document: Web Crawler + Search Engine ("Google in a Day")

**Version:** 1.0
**Date:** 2026-03-16
**Status:** Draft

---

## 1. Overview

Build a working web crawler and search engine from scratch using Python. The system has three parts: an indexer (crawler), a searcher, and a web dashboard. The goal is to show how a basic search engine works end-to-end without relying on third-party libraries for the core logic.

---

## 2. Goals

- Demonstrate how a search engine indexes and retrieves content at a small scale.
- Build concurrency-safe infrastructure using Python's `threading` module.
- Support live querying while a crawl is still in progress.
- Enforce back pressure to prevent memory overflow during large crawls.

---

## 3. Non-Goals

- This is not a production-grade search engine.
- No distributed crawling, no external databases, no cloud storage.
- No JavaScript rendering or dynamic page support (static HTML only).
- No authentication, user accounts, or persistent sessions.

---

## 4. Functional Requirements

### 4.1 Indexer (Crawler)

| ID | Requirement |
|----|-------------|
| F-01 | Accept an **origin URL** and **depth k** as input parameters. |
| F-02 | Recursively crawl all reachable pages up to depth k from the origin. |
| F-03 | Never crawl the same URL twice. The visited set must be thread-safe. |
| F-04 | Enforce a **maximum queue depth of 500 URLs** to apply back pressure. |
| F-05 | Enforce a **rate limit of max 5 URLs/second per job** via `time.sleep(1/rate_limit)`. Multiple concurrent jobs each apply their own rate limit independently. |
| F-06 | Extract and store a **word frequency index** for each crawled page. |
| F-07 | Parse the page title during crawling; index title tokens with 3× weight (not stored as a separate metadata field). |
| F-08 | Log crawl state to a JSON file named `[epochtime_uuid8].data` in the `data/` directory. |
| F-09 | Handle HTTP errors (4xx, 5xx), timeouts, and malformed URLs gracefully without crashing. |
| F-10 | On stop signal, worker exits at the next safe checkpoint and persists final state. Queue is not fully drained; remaining URLs are discarded. |

### 4.2 Searcher

| ID | Requirement |
|----|-------------|
| F-11 | Accept a **query string** as input. |
| F-12 | Return a ranked list keyed by `(relevant_url, origin_url, depth)` triples; same URL discovered under different origin/depth combinations appears as separate results. |
| F-13 | Rank results by **keyword frequency** using: `relevance_score = (frequency × 10) + 1000 - (depth × 5)`. |
| F-14 | Title tokens are weighted 3× during indexing in `crawler.py` to boost title-matching URLs. |
| F-15 | Support multi-word queries; rank by combined frequency of all query terms. |
| F-16 | Operate concurrently with the indexer — must return results while a crawl is still running. |
| F-17 | Return an empty result set (not an error) if no matching pages are found. |

### 4.3 Web Dashboard

**Page 1 — Start Crawl**

| ID | Requirement |
|----|-------------|
| F-18 | Input form for origin URL and depth k. |
| F-19 | Validate that the URL is well-formed before starting the crawl. |
| F-20 | Display a confirmation or error message after submission. |
| F-21 | Prevent starting a new crawl if one is already running (or clearly indicate concurrent jobs). |

**Page 2 — Crawler Status**

| ID | Requirement |
|----|-------------|
| F-22 | Show real-time crawler status via two independent loops: stat cards poll `/api/crawl/{id}/status` every 1 s; live log streams via long-poll on `/api/status/poll/{id}`. |
| F-23 | Show queue depth, URLs processed, URLs/sec, and whether back-pressure is active. |
| F-24 | Show the crawl job ID. |
| F-25 | No full page reloads — updates should happen in-place. |

**Page 3 — Search**

| ID | Requirement |
|----|-------------|
| F-26 | Input field to enter a search query. |
| F-27 | Display paginated results: URL, origin URL, depth, and a relevance score or snippet. |
| F-28 | Support at minimum 10 results per page with next/previous navigation. |
| F-29 | Show total result count and time taken for the query. |

---

## 5. Technical Constraints

| Constraint | Requirement |
|------------|-------------|
| Language | Python 3.11+ only |
| HTTP | `urllib.request`, `urllib.parse` — no `requests` library |
| HTML Parsing | `html.parser` from stdlib — no BeautifulSoup, lxml, or Scrapy |
| Concurrency | `threading` module; all shared data structures protected by `threading.RLock()` |
| Web Server | `http.server` from stdlib OR Flask (for serving only, not crawling) |
| Storage | Filesystem only — plain text line-based shards under `data/storage/`; crawler state in JSON under `data/` |
| No external packages | Except Flask if used for the web server |

---

## 6. Data Structures

### 6.1 `visited_urls`
- **Type:** `set`
- **Protection:** `threading.RLock()`
- **Purpose:** Prevent duplicate crawls. Check-and-add must be atomic.

### 6.2 `url_queue`
- **Type:** `queue.Queue(maxsize=500)`
- **Purpose:** Buffer URLs to be crawled. `maxsize=500` provides back pressure — URLs are dropped when the queue is full (`put_nowait` pattern) to prevent unbounded memory growth.

### 6.3 `index_store`
- **Type:** `dict` mapping `word (str)` → `list of (url, origin_url, depth, frequency)`
- **Protection:** `threading.RLock()`
- **Purpose:** The core inverted index. Readers (searcher) and writers (crawler) share this structure concurrently.

### 6.4 Crawler State File
- **Format:** JSON
- **Filename:** `[epochtime_uuid8].data`
- **Location:** `data/`
- **Fields:** `crawler_id`, `origin_url`, `max_depth`, `status`, `urls_processed`, `urls_per_second`, `queue_depth`, `max_queue_size`, `back_pressure_active`, `logs`

### 6.5 Word Index Shards
- **Format:** Plain text, one line per entry: `word url origin_url depth frequency`
- **Location:** `data/storage/`
- **Sharding:** One file per first letter (`a.data` … `z.data`)
- **Writes:** Atomic via `.tmp` + `os.replace()`

---

## 7. File Structure

```
google-crawler/
├── crawler.py          # Core crawler logic: URL fetching, HTML parsing, index building
├── searcher.py         # Search engine: query parsing, index lookup, ranking
├── server.py           # Web server: HTTP endpoints, long polling, template rendering
├── templates/          # HTML templates for the three dashboard pages
│   ├── index.html      # Page 1: Start crawl
│   ├── status.html     # Page 2: Crawler status
│   └── search.html     # Page 3: Search results
├── data/storage/       # Word index shards (plain text, a.data … z.data)
├── data/               # Crawler job state files ([epochtime_uuid8].data, JSON)
├── product_prd.md      # This document
├── README.md           # Setup and usage instructions
└── recommendation.md   # Scaling and improvement notes
```

---

## 8. API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Render Page 1 (start crawl form) |
| `POST` | `/api/crawl` | Start a new crawl job |
| `GET` | `/status/<crawler_id>` | Render Page 2 (status dashboard) |
| `GET` | `/api/status/poll/<crawler_id>` | Long-poll endpoint returning JSON crawler state |
| `GET` | `/search` | Render Page 3 (search form + results) |
| `GET` | `/api/search?q=...&page=N` | Return paginated search results as JSON |

---

## 9. Concurrency Model

```
Main Thread
  └── HTTP Server (serves dashboard + API)

Crawler Thread (one per job)
  └── dequeue URL → fetch → parse → update index_store + visited_urls

Rate Limiter (per job)
  └── time.sleep(1/rate_limit) between fetches — concurrent jobs each apply their own limit independently

Back Pressure
  └── bounded queue with put_nowait(); URLs dropped when full, not blocked — prevents unbounded memory growth
```

All writes to `index_store` and `visited_urls` need to hold the corresponding `RLock`. The searcher also acquires the lock when reading so it doesn't see a half-updated state.

---

## 10. Success Criteria

| Criterion | Pass Condition |
|-----------|----------------|
| Crawl stability | Crawl a real website to depth 2 without crashing or infinite loops |
| Live search | Search returns results while a crawl is actively running |
| Back pressure | Queue never exceeds 500 items; URLs are dropped when full (`put_nowait`) to prevent unbounded memory growth |
| Thread safety | No race conditions, no data corruption under concurrent read/write |
| Rate limiting | Crawler does not exceed 5 HTTP requests/second |
| State persistence | Each crawl job writes a valid `.data` JSON file; visited URLs saved to disk as a per-job artifact. Queue is not persisted. There is no resume workflow in v1 — each `start()` call creates a fresh job. |
| Paginated results | Search UI returns at least 10 results per page with navigation |

---

## 11. Out of Scope

- HTTPS certificate verification bypass or custom TLS handling
- robots.txt compliance (recommended but not required for v1)
- Stemming, stop-word filtering, or TF-IDF ranking
- Persistent index across server restarts (in-memory only for v1)
- Any form of authentication or access control

---

## 12. Open Questions

1. Should the crawler respect `robots.txt`? Not currently implemented; recommended for ethical use.
2. Multiple concurrent jobs are supported — each has its own visited set and rate limiter.
3. Visited URLs are saved to disk as a per-job artifact (not a resume mechanism — the queue is not persisted and `start()` always creates a fresh job).

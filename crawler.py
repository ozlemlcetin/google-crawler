"""
crawler.py — the core crawl engine.

Fetches pages up to a given depth, builds a word-frequency index, saves
everything to disk. Runs in a background thread so the server stays responsive.
"""

import json
import os
import queue
import threading
import time
import uuid
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


# ---------------------------------------------------------------------------
# HTML Parsers
# ---------------------------------------------------------------------------

class _LinkParser(HTMLParser):
    """Pull all href values out of <a> tags."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)


class _TextParser(HTMLParser):
    """Extract visible text and the <title> from a page. Skips script/style."""

    # Tags whose full content (open→close) should be ignored.
    # Void elements (meta, link) produce no text data, so they need no entry here.
    _SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0   # depth counter — handles nested skips robustly
        self._text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        else:
            self._text_parts.append(text)

    @property
    def text(self):
        return " ".join(self._text_parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenise(text):
    """Break text into lowercase alpha tokens. Numbers and punctuation are ignored."""
    tokens = []
    current = []
    for ch in text.lower():
        if ch.isalpha():
            current.append(ch)
        else:
            if current:
                tokens.append("".join(current))
                current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _log_entry(crawler_id, event, data=None):
    """Build a log dict. Keeping it structured so the frontend can parse it easily."""
    return {
        "timestamp": time.time(),
        "crawler_id": crawler_id,
        "event": event,
        "data": data or {},
    }


# ---------------------------------------------------------------------------
# Main crawler class
# ---------------------------------------------------------------------------

class CrawlerJob:
    """One crawl job. Call start() and it runs in the background.

    job = CrawlerJob("https://example.com", max_depth=2)
    crawler_id = job.start()
    """

    # Registry of all live/completed jobs keyed by crawler_id.
    all_jobs: dict = {}

    # Lock that protects all_jobs itself.
    _registry_lock = threading.RLock()

    def __init__(self, origin_url: str, max_depth: int,
                 max_queue_size: int = 500, rate_limit: int = 5,
                 resume_frontier: list | None = None,
                 resume_visited: set | None = None):
        """Set up the job. Doesn't start crawling yet — call start() for that.

        resume_frontier: list of (url, origin_url, depth) tuples to pre-seed the queue.
        resume_visited:  set of URLs already crawled in the previous job run.
        Both are provided by from_saved_state(); leave None for a fresh crawl.
        """
        self.origin_url = origin_url
        self.max_depth = max_depth
        self.max_queue_size = max_queue_size
        self.rate_limit = rate_limit

        # Assigned in start()
        self.crawler_id: str = ""

        # --- Shared state (all protected by their respective locks) ---
        self.visited_urls: set = set()
        self.enqueued_urls: set = set()  # same-job "already queued" guard
        self._visited_lock = threading.RLock()

        # index_store: word -> [{"url", "origin_url", "depth", "frequency"}, ...]
        self.index_store: dict = {}
        self._index_lock = threading.RLock()

        # URL work queue with back-pressure
        self.url_queue: queue.Queue = queue.Queue(maxsize=max_queue_size)

        # --- Mutable status fields ---
        self.status: str = "pending"   # pending / running / done / error
        self.urls_processed: int = 0
        self.back_pressure_active: bool = False
        self._logs: list = []
        self._status_lock = threading.RLock()

        # Stop flag and start time for metrics
        self._stop_event = threading.Event()
        self._start_time: float | None = None

        # --- Resume state (provided by from_saved_state(); empty for a fresh crawl) ---
        # _resume_frontier: (url, origin_url, depth) tuples to pre-seed the queue
        # _resume_visited:  URLs already crawled — skip them on the resumed run
        self._resume_frontier: list = list(resume_frontier) if resume_frontier else []
        self._resume_visited: set = set(resume_visited) if resume_visited else set()

        # Pending URL frontier: url -> {origin_url, depth}.
        # Tracks what is currently in the queue so we can persist and restore it
        # if the process is interrupted before the crawl finishes.
        self._frontier_items: dict = {}

        # Ensure storage directories exist
        os.makedirs("data", exist_ok=True)
        os.makedirs(os.path.join("data", "storage"), exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> str:
        """Kick off the crawl in a background thread and return a crawler_id.

        The ID is epoch_uuid8 — human-readable with lower collision risk than timestamp+thread-ident.
        For a resumed crawl (from_saved_state()), the queue is pre-seeded from the saved frontier
        and visited_urls is pre-populated so already-crawled pages are skipped.
        """
        self.crawler_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"

        with CrawlerJob._registry_lock:
            CrawlerJob.all_jobs[self.crawler_id] = self

        if self._resume_visited:
            # Resume path: pre-populate visited/enqueued from the saved snapshot.
            with self._visited_lock:
                self.visited_urls.update(self._resume_visited)
                self.enqueued_urls.update(self._resume_visited)
            self._log("Resuming crawl", {
                "origin": self.origin_url,
                "max_depth": self.max_depth,
                "skipping_visited": len(self._resume_visited),
            })
        else:
            # Fresh crawl: load any existing visited snapshot (normally empty for a new id).
            self._load_visited_urls()
            self._log(f"Crawl job created — origin={self.origin_url}, depth={self.max_depth}")

        # Seed the queue: from saved frontier (resume) or from origin URL (fresh start).
        if self._resume_frontier:
            seeded = 0
            for r_url, r_origin, r_depth in self._resume_frontier:
                if r_url in self.visited_urls:
                    continue  # already crawled in the previous run
                try:
                    self.url_queue.put_nowait((r_url, r_origin, r_depth))
                    with self._visited_lock:
                        self.enqueued_urls.add(r_url)
                        self._frontier_items[r_url] = {"origin_url": r_origin, "depth": r_depth}
                    seeded += 1
                except queue.Full:
                    break  # queue full — remaining frontier items dropped (back-pressure)
            if seeded == 0:
                # Entire frontier was already visited or the queue was full from the start.
                # Fall back to re-crawling from the origin so the job isn't a no-op.
                self.url_queue.put((self.origin_url, self.origin_url, 0))
                with self._visited_lock:
                    self.enqueued_urls.add(self.origin_url)
                    self._frontier_items[self.origin_url] = {"origin_url": self.origin_url, "depth": 0}
            self._log("Resume: frontier seeded", {"urls_seeded": seeded})
        else:
            # Normal fresh start: seed with the origin URL only.
            self.url_queue.put((self.origin_url, self.origin_url, 0))
            with self._visited_lock:
                self.enqueued_urls.add(self.origin_url)
                self._frontier_items[self.origin_url] = {"origin_url": self.origin_url, "depth": 0}

        with self._status_lock:
            self.status = "running"
            self._start_time = time.time()

        worker = threading.Thread(
            target=self._crawl_worker,
            name=f"crawler-{self.crawler_id}",
            daemon=True,
        )
        worker.start()

        return self.crawler_id

    def stop(self):
        """Tell the worker to stop after it finishes the current URL."""
        self._stop_event.set()

    def get_state(self) -> dict:
        """Snapshot of current state — safe to call from any thread."""
        with self._status_lock:
            elapsed = time.time() - self._start_time if self._start_time is not None else 1
            urls_per_second = round(self.urls_processed / elapsed, 2) if elapsed > 0 else 0
            return {
                "crawler_id": self.crawler_id,
                "status": self.status,
                "origin_url": self.origin_url,
                "max_depth": self.max_depth,
                "rate_limit": self.rate_limit,
                "urls_processed": self.urls_processed,
                "urls_per_second": urls_per_second,
                "queue_depth": self.url_queue.qsize(),
                "max_queue_size": self.max_queue_size,
                "back_pressure_active": self.back_pressure_active,
                "logs": list(self._logs[-100:]),  # last 100 log entries
            }

    # ------------------------------------------------------------------
    # Internal: crawl loop
    # ------------------------------------------------------------------

    def _crawl_worker(self):
        """The actual crawl loop. Runs until the queue drains, we time out, or stop() is called."""
        self._log("Crawl worker started")

        while True:
            if self._stop_event.is_set():
                self._log("Crawl stopped by user request")
                break

            try:
                url, origin, depth = self.url_queue.get(timeout=1)
            except queue.Empty:
                # No URLs for 1 second — queue is empty, consider crawl complete
                break

            # Remove from frontier — this URL is now being processed (no longer pending).
            with self._visited_lock:
                self._frontier_items.pop(url, None)

            if self._stop_event.is_set():
                self.url_queue.task_done()
                self._log("Crawl stopped by user request")
                break

            # Skip if already visited
            with self._visited_lock:
                if url in self.visited_urls:
                    self.url_queue.task_done()
                    continue
                self.visited_urls.add(url)

            self._log("Fetching URL", {"url": url, "depth": depth})

            # Rate limiting
            time.sleep(1 / self.rate_limit)

            html_text, status_code = self._fetch_page(url)

            if html_text is None:
                self._log("Fetch failed", {"url": url, "status_code": status_code})
                self.url_queue.task_done()
                self._save_state()
                continue

            self._log("Fetch OK", {"url": url, "status_code": status_code})

            # Index words from this page
            word_freq, title = self._parse_words(html_text)
            self._update_index(word_freq, url=url, origin_url=origin, depth=depth)
            self._save_index(word_freq)

            with self._status_lock:
                self.urls_processed += 1

            # Enqueue discovered links if we haven't hit max depth
            if depth < self.max_depth:
                links = self._parse_links(html_text, url)
                for link in links:
                    with self._visited_lock:
                        already_seen = link in self.visited_urls or link in self.enqueued_urls
                    if already_seen:
                        continue
                    try:
                        self.url_queue.put_nowait((link, origin, depth + 1))
                        with self._visited_lock:
                            self.enqueued_urls.add(link)
                            # Track in frontier so this URL can be restored on resume.
                            self._frontier_items[link] = {"origin_url": origin, "depth": depth + 1}
                        with self._status_lock:
                            self.back_pressure_active = False
                    except queue.Full:
                        with self._status_lock:
                            self.back_pressure_active = True
                        self._log("Back pressure: queue full, skipping URL", {"url": link})

            self._persist_visited_urls()
            self._save_state()
            self._save_frontier()  # persist pending queue for resume support
            self.url_queue.task_done()

        with self._status_lock:
            self.status = "stopped" if self._stop_event.is_set() else "done"
            self.back_pressure_active = False

        self._log("Crawl worker finished", {"urls_processed": self.urls_processed})
        self._save_state()

    # ------------------------------------------------------------------
    # Internal: fetch
    # ------------------------------------------------------------------

    def _fetch_page(self, url: str) -> tuple[str | None, int | None]:
        """Fetch a URL and return (html, status_code). Returns (None, code) on any error."""
        try:
            req = Request(
                url,
                headers={"User-Agent": "GoogleInADay/1.0 (+educational-crawler)"},
            )
            with urlopen(req, timeout=10) as response:
                status_code = response.status
                content_type = response.headers.get("Content-Type", "")
                if "text/html" not in content_type:
                    return None, status_code
                raw = response.read()
                # Try utf-8 first, fall back to latin-1
                try:
                    html_text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    html_text = raw.decode("latin-1", errors="replace")
                return html_text, status_code
        except HTTPError as exc:
            return None, exc.code
        except URLError as exc:
            self._log("URLError", {"url": url, "reason": str(exc.reason)})
            return None, None
        except Exception as exc:  # noqa: BLE001
            self._log("Unexpected fetch error", {"url": url, "error": str(exc)})
            return None, None

    # ------------------------------------------------------------------
    # Internal: parsing
    # ------------------------------------------------------------------

    def _parse_links(self, html: str, base_url: str) -> list[str]:
        """Find all links in the page, resolve relative ones, keep only same-domain http(s)."""
        parser = _LinkParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001
            return []

        def _strip_www(host: str) -> str:
            return host[4:] if host.startswith("www.") else host

        base_netloc = _strip_www(urlparse(base_url).netloc)
        links = []
        seen = set()
        skipped_hosts: dict = {}  # host -> count, for observability

        for href in parser.links:
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)

            # Keep only http(s), same domain (www-normalised), no fragments
            if parsed.scheme not in ("http", "https"):
                continue
            if _strip_www(parsed.netloc) != base_netloc:
                if parsed.netloc:
                    skipped_hosts[parsed.netloc] = skipped_hosts.get(parsed.netloc, 0) + 1
                continue

            # Strip fragment and trailing slash for deduplication
            normalised = parsed._replace(fragment="").geturl().rstrip("/")
            if normalised not in seen:
                seen.add(normalised)
                links.append(normalised)

        if skipped_hosts:
            self._log(
                "Links skipped (different host — crawler is same-host scoped)",
                {"scope_host": base_netloc, "skipped": skipped_hosts},
            )

        return links

    def _parse_words(self, html: str) -> tuple[dict, str]:
        """Parse visible text from the page, return (word_freq_dict, title).

        Title words get 3x weight — a bit hacky but works well enough in practice.
        """
        parser = _TextParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001
            return {}, ""

        tokens = _tokenise(parser.text)
        title_tokens = _tokenise(parser.title)  # gets 3× weight below

        freq: dict = {}
        for token in tokens:
            if len(token) < 2:
                continue
            freq[token] = freq.get(token, 0) + 1

        for token in title_tokens:
            if len(token) < 2:
                continue
            freq[token] = freq.get(token, 0) + 3

        return freq, parser.title

    # ------------------------------------------------------------------
    # Internal: index management
    # ------------------------------------------------------------------

    def _update_index(self, word_freq: dict, url: str,
                      origin_url: str, depth: int):
        """Add this page's word frequencies into the shared in-memory index.

        Skips any (url, origin_url, depth) triple already present. This prevents
        relevance-score inflation when the same page is crawled by a second job
        (e.g., crawling the same site twice in the same server session).
        """
        with self._index_lock:
            for word, frequency in word_freq.items():
                if word not in self.index_store:
                    self.index_store[word] = []
                # Dedup: if this exact page+depth combo is already in the index, skip it.
                if any(e["url"] == url and e.get("origin_url") == origin_url
                       and e.get("depth") == depth for e in self.index_store[word]):
                    continue
                entry = {
                    "url": url,
                    "origin_url": origin_url,
                    "depth": depth,
                    "frequency": frequency,
                }
                self.index_store[word].append(entry)

    def _save_index(self, word_freq: dict):
        """Write updated index shards to disk for all first-letters in word_freq.

        Format: one line per entry — "word url origin_url depth frequency"
        Each shard is storage/{letter}.data. Written atomically via .tmp + rename.

        Snapshots the relevant index data under the lock first, then releases the lock
        before doing any disk I/O so other threads are not blocked during the write.
        """
        letters = {word[0] for word in word_freq if word and word[0].isalpha()}

        # Snapshot under lock — I/O happens outside the lock below.
        with self._index_lock:
            shards_to_write: dict = {}
            for letter in letters:
                lines = []
                for word, entries in self.index_store.items():
                    if not word or word[0] != letter:
                        continue
                    for entry in entries:
                        url = entry.get("url", "")
                        origin = entry.get("origin_url", "")
                        depth = entry.get("depth", 0)
                        freq = entry.get("frequency", 0)
                        lines.append(f"{word} {url} {origin} {depth} {freq}")
                shards_to_write[letter] = lines

        # Disk writes happen outside the lock — keeps the critical section short.
        for letter, lines in shards_to_write.items():
            path = os.path.join("data", "storage", f"{letter}.data")
            tmp_path = path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
                os.replace(tmp_path, path)
            except OSError as exc:
                self._log("Index save error", {"letter": letter, "error": str(exc)})

    # ------------------------------------------------------------------
    # Internal: state persistence
    # ------------------------------------------------------------------

    def _save_state(self):
        """Flush current state to data/{crawler_id}.data so the API can read it."""
        if not self.crawler_id:
            return
        path = os.path.join("data", f"{self.crawler_id}.data")
        state = self.get_state()
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp_path, path)
        except OSError as exc:
            self._log("State save error", {"error": str(exc)})

    def _visited_path(self) -> str:
        """Path to this job's visited-URLs file."""
        return os.path.join("data", f"{self.crawler_id}_visited.data")

    def _persist_visited_urls(self):
        """Write visited URLs to disk as a per-job snapshot. Used by from_saved_state() on resume."""
        path = self._visited_path()
        tmp_path = path + ".tmp"
        try:
            with self._visited_lock:
                urls = list(self.visited_urls)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(urls))
            os.replace(tmp_path, path)
        except OSError as exc:
            self._log("visited_urls persist error", {"error": str(exc)})

    def _load_visited_urls(self):
        """Load previously visited URLs for this job, if any exist."""
        path = self._visited_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                urls = {line.strip() for line in fh if line.strip()}
            with self._visited_lock:
                self.visited_urls.update(urls)
        except OSError as exc:
            print(f"[crawler] Could not load {path}: {exc}")

    def _save_frontier(self):
        """Persist the current pending URL frontier to disk for resume support.

        Saves url -> {origin_url, depth} for all URLs currently in the queue.
        Called after every page so an interrupted crawl can be resumed from a
        recent snapshot of what still needs to be fetched.
        """
        if not self.crawler_id:
            return
        path = os.path.join("data", f"{self.crawler_id}_frontier.data")
        tmp_path = path + ".tmp"
        try:
            with self._visited_lock:
                frontier_snapshot = dict(self._frontier_items)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(frontier_snapshot, fh)
            os.replace(tmp_path, path)
        except OSError as exc:
            self._log("Frontier save error", {"error": str(exc)})

    # ------------------------------------------------------------------
    # Class method: resume a previous job
    # ------------------------------------------------------------------

    @classmethod
    def from_saved_state(cls, original_crawler_id: str) -> "CrawlerJob":
        """Create a new CrawlerJob pre-populated from a previous job's saved frontier
        and visited-URL set.

        The returned job has a fresh crawler_id (assigned by start()). The caller
        must wire in index_store and _index_lock before calling start().

        Raises ValueError if the state file cannot be read or the job already completed.
        """
        state_path = os.path.join("data", f"{original_crawler_id}.data")
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot load state for {original_crawler_id}: {exc}") from exc

        if state.get("status") == "done":
            raise ValueError("Cannot resume a completed crawl (status: done)")

        # Load the persisted visited-URL snapshot.
        resume_visited: set = set()
        visited_path = os.path.join("data", f"{original_crawler_id}_visited.data")
        if os.path.exists(visited_path):
            try:
                with open(visited_path, "r", encoding="utf-8") as fh:
                    resume_visited = {line.strip() for line in fh if line.strip()}
            except OSError:
                pass

        # Load the persisted frontier: dict of url -> {origin_url, depth}.
        resume_frontier: list = []
        frontier_path = os.path.join("data", f"{original_crawler_id}_frontier.data")
        if os.path.exists(frontier_path):
            try:
                with open(frontier_path, "r", encoding="utf-8") as fh:
                    frontier_data: dict = json.load(fh)
                resume_frontier = [
                    (url, info["origin_url"], info["depth"])
                    for url, info in frontier_data.items()
                    if url and isinstance(info, dict)
                    and "origin_url" in info and "depth" in info
                ]
            except (OSError, json.JSONDecodeError, KeyError):
                pass  # frontier file missing or corrupt — will re-crawl from origin

        return cls(
            origin_url=state["origin_url"],
            max_depth=state["max_depth"],
            max_queue_size=state.get("max_queue_size", 500),
            rate_limit=state.get("rate_limit", 5),
            resume_frontier=resume_frontier,
            resume_visited=resume_visited,
        )

    # ------------------------------------------------------------------
    # Internal: logging
    # ------------------------------------------------------------------

    def _log(self, event: str, data: dict | None = None):
        """Append a log entry and print it. The frontend polls these to update the dashboard."""
        entry = _log_entry(self.crawler_id or "unassigned", event, data)
        with self._status_lock:
            self._logs.append(entry)
        print(json.dumps(entry))

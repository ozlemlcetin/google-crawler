"""
searcher.py — takes a query, looks it up in the index, returns ranked results.

Searches both the live in-memory index (requires the lock) and the on-disk shards
under data/storage/ (no lock needed — written atomically by the crawler).

Results are keyed by (relevant_url, origin_url, depth) so the same URL discovered
through different crawl paths appears as separate entries with their own scores.

Scoring: relevance_score = (frequency × 10) + 1000 - (depth × 5)
Title tokens are indexed at 3× weight by the crawler, so title matches rank higher.
"""

import os
import threading


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenise_query(query: str) -> list[str]:
    """Split query into lowercase tokens, toss anything under 2 chars."""
    tokens = []
    current = []
    for ch in query.lower():
        if ch.isalpha():
            current.append(ch)
        else:
            if current:
                token = "".join(current)
                if len(token) >= 2 and token not in tokens:
                    tokens.append(token)
                current = []
    if current:
        token = "".join(current)
        if len(token) >= 2 and token not in tokens:
            tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------

def load_index_for_letter(letter: str) -> dict:
    """Read data/storage/{letter}.data from disk.
    Format: plain text, one line per entry — 'word url origin_url depth frequency'.
    Returns dict mapping word -> list of entry dicts."""
    if not letter or not letter.isalpha():
        return {}

    path = os.path.join("data", "storage", f"{letter.lower()}.data")
    if not os.path.exists(path):
        return {}

    result: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(" ", 4)
                if len(parts) != 5:
                    continue
                word, url, origin_url, depth_str, freq_str = parts
                try:
                    depth = int(depth_str)
                    frequency = int(freq_str)
                except ValueError:
                    continue
                entry = {"url": url, "origin_url": origin_url, "depth": depth, "frequency": frequency}
                if word not in result:
                    result[word] = []
                result[word].append(entry)
    except OSError:
        return {}
    return result


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------

def search(query: str, index_store: dict, index_lock: threading.RLock) -> list[dict]:
    """Look up query tokens across the live in-memory index and on-disk shards,
    merge by (url, origin_url, depth) triple, score, and return sorted results.

    Scoring formula: relevance_score = (frequency × 10) + 1000 - (depth × 5)
    Each query token contributes independently; scores accumulate per triple.
    """
    tokens = _tokenise_query(query)
    if not tokens:
        return []

    # Key = (url, origin_url, depth) — same URL found via different origins stays separate.
    scores: dict = {}

    for token in tokens:
        # --- 1. Read from in-memory index_store (requires the lock) ---
        with index_lock:
            memory_entries = list(index_store.get(token, []))

        # --- 2. Read from on-disk shard (no lock needed — atomic writes) ---
        first_letter = token[0]
        shard = load_index_for_letter(first_letter)
        disk_entries = shard.get(token, [])

        # Merge: prefer memory entries; use disk as fallback for triples not yet
        # reflected in memory (e.g. from a previous crawl session).
        memory_keys = {(e["url"], e.get("origin_url", ""), e.get("depth", 0)) for e in memory_entries}
        combined = memory_entries + [
            e for e in disk_entries
            if (e["url"], e.get("origin_url", ""), e.get("depth", 0)) not in memory_keys
        ]

        for entry in combined:
            url = entry.get("url", "")
            if not url:
                continue

            frequency = entry.get("frequency", 0)
            origin_url = entry.get("origin_url", "")
            depth = entry.get("depth", 0)

            delta = (frequency * 10) + 1000 - (depth * 5)

            key = (url, origin_url, depth)
            if key not in scores:
                scores[key] = {"relevance_score": 0.0}
            scores[key]["relevance_score"] += delta

    # Build and sort result list
    results = [
        {
            "relevant_url": key[0],
            "origin_url": key[1],
            "depth": key[2],
            "relevance_score": meta["relevance_score"],
        }
        for key, meta in scores.items()
    ]
    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def paginate(results: list[dict], page: int, per_page: int = 20) -> dict:
    """Slice results for one page. Pages are 1-based; out-of-range pages just return empty items."""
    if per_page < 1:
        per_page = 20
    page = max(1, page)

    total_count = len(results)
    total_pages = max(1, (total_count + per_page - 1) // per_page)

    start = (page - 1) * per_page
    end = start + per_page
    items = results[start:end]

    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    }

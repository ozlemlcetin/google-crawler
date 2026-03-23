"""
server.py — Flask layer on top of crawler.py and searcher.py.

Three HTML pages + a JSON API. All jobs share one index so search works
even while a crawl is running.
"""

import json
import os
import time
import threading
from urllib.parse import urlparse

from flask import Flask, jsonify, request, render_template, abort

from crawler import CrawlerJob
from searcher import search, paginate


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates")

# Shared index and its lock — injected into every CrawlerJob so all jobs
# contribute to (and are searchable through) a single index.
shared_index_store: dict = {}
shared_index_lock: threading.RLock = threading.RLock()

# Ensure storage directories exist at startup
os.makedirs("data", exist_ok=True)
os.makedirs(os.path.join("data", "storage"), exist_ok=True)


# ---------------------------------------------------------------------------
# CORS — allow all origins for every response
# ---------------------------------------------------------------------------

@app.after_request
def add_cors_headers(response):
    """Slap CORS headers on everything — makes local dev easier."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------

@app.route("/")
def page_index():
    return render_template("index.html")


@app.route("/status/<crawler_id>")
def page_status(crawler_id):
    return render_template("status.html", crawler_id=crawler_id)


@app.route("/search")
def page_search():
    """Return JSON results if ?query= is present, otherwise render the search page."""
    query = request.args.get("query", request.args.get("q", "")).strip()
    if not query:
        return render_template("search.html")

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 20))
    except ValueError:
        per_page = 20

    t0 = time.perf_counter()
    all_results = search(query, shared_index_store, shared_index_lock)
    page_data = paginate(all_results, page=page, per_page=per_page)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

    results = page_data["items"]
    return jsonify({
        "results": results,
        "total": page_data["total_count"],
        "page": page_data["page"],
        "per_page": page_data["per_page"],
        "total_pages": page_data["total_pages"],
        "elapsed_ms": elapsed_ms,
    }), 200


# ---------------------------------------------------------------------------
# API — crawl management
# ---------------------------------------------------------------------------

@app.route("/api/crawl", methods=["POST", "OPTIONS"])
def api_start_crawl():
    """Start a new crawl. Expects JSON: {origin_url, max_depth, rate_limit?, max_queue_size?}.
    Returns {crawler_id} on success."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    body = request.get_json(silent=True) or {}

    origin_url = body.get("origin_url", "").strip()
    max_depth = body.get("max_depth")

    try:
        rate_limit = int(body.get("rate_limit", 5))
    except (TypeError, ValueError):
        rate_limit = 5
    if rate_limit <= 0 or rate_limit > 50:
        rate_limit = 5

    try:
        max_queue_size = int(body.get("max_queue_size", 500))
    except (TypeError, ValueError):
        max_queue_size = 500
    if max_queue_size < 10 or max_queue_size > 5000:
        max_queue_size = 500

    if not origin_url:
        return jsonify({"error": "origin_url is required"}), 400

    parsed = urlparse(origin_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"error": "Invalid URL"}), 400

    if max_depth is None:
        return jsonify({"error": "max_depth is required"}), 400

    try:
        max_depth = int(max_depth)
    except (TypeError, ValueError):
        return jsonify({"error": "max_depth must be an integer"}), 400

    try:
        job = CrawlerJob(
            origin_url=origin_url,
            max_depth=max_depth,
            max_queue_size=max_queue_size,
            rate_limit=rate_limit,
        )
        # Wire the shared index into this job so all jobs feed the same index.
        job.index_store = shared_index_store
        job._index_lock = shared_index_lock

        crawler_id = job.start()
        return jsonify({"crawler_id": crawler_id}), 201

    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/crawl/<crawler_id>/status", methods=["GET"])
def api_crawl_status(crawler_id):
    """Read data/{crawler_id}.data off disk and return it. Always the last saved snapshot."""
    path = os.path.join("data", f"{crawler_id}.data")
    if not os.path.exists(path):
        return jsonify({"error": "crawler not found"}), 404

    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        return jsonify(state), 200
    except (json.JSONDecodeError, OSError) as exc:
        return jsonify({"error": f"could not read state file: {exc}"}), 500


@app.route("/api/crawl/<crawler_id>/stop", methods=["POST"])
def api_stop_crawl(crawler_id):
    """Tell the crawler to stop. It'll finish the current URL then exit cleanly."""
    with CrawlerJob._registry_lock:
        job = CrawlerJob.all_jobs.get(crawler_id)

    if job is None:
        return jsonify({"error": "crawler not found"}), 404

    job.stop()
    return jsonify({"crawler_id": crawler_id, "status": "stopping"}), 200


@app.route("/api/crawl/list", methods=["GET"])
def api_crawl_list():
    """List all known crawl jobs — in-memory ones first, then anything on disk from previous runs."""
    jobs = []
    seen_ids = set()

    # In-memory jobs first (most up-to-date)
    with CrawlerJob._registry_lock:
        registry_snapshot = dict(CrawlerJob.all_jobs)

    for crawler_id, job in registry_snapshot.items():
        seen_ids.add(crawler_id)
        jobs.append({
            "crawler_id": crawler_id,
            "status": job.status,
            "origin_url": job.origin_url,
            "urls_processed": job.urls_processed,
        })

    # Supplement with any .data files not in memory (previous sessions).
    # Skip *_visited.data files — those are plain-text URL lists, not job state.
    try:
        for filename in os.listdir("data"):
            if not filename.endswith(".data") or filename.endswith("_visited.data"):
                continue
            crawler_id = filename[:-5]
            if crawler_id in seen_ids:
                continue
            path = os.path.join("data", filename)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                jobs.append({
                    "crawler_id": crawler_id,
                    "status": state.get("status", "unknown"),
                    "origin_url": state.get("origin_url", ""),
                    "urls_processed": state.get("urls_processed", 0),
                })
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass

    # Sort newest-first. crawler_id starts with epoch timestamp so
    # reverse-lexicographic order gives the most recent jobs at the top.
    jobs.sort(key=lambda j: j["crawler_id"], reverse=True)

    return jsonify({"jobs": jobs}), 200


# ---------------------------------------------------------------------------
# API — search
# ---------------------------------------------------------------------------

@app.route("/api/search", methods=["GET"])
def api_search():
    """Search the index. Params: q (required), page, per_page."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1

    try:
        per_page = int(request.args.get("per_page", 20))
    except ValueError:
        per_page = 20

    t0 = time.perf_counter()
    all_results = search(query, shared_index_store, shared_index_lock)
    page_data = paginate(all_results, page=page, per_page=per_page)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

    results = page_data["items"]
    return jsonify({
        "results": results,
        "total": page_data["total_count"],
        "page": page_data["page"],
        "per_page": page_data["per_page"],
        "total_pages": page_data["total_pages"],
        "elapsed_ms": elapsed_ms,
    }), 200


# ---------------------------------------------------------------------------
# API — long polling
# ---------------------------------------------------------------------------

@app.route("/api/status/poll/<crawler_id>", methods=["GET"])
def api_poll_status(crawler_id):
    """Long-poll for live status. Holds the connection up to 20s waiting for new log entries.
    Client passes ?since=N (log entries already seen) and gets back only the new ones.

    # this is a bit hacky but avoids WebSockets and works fine for single-machine scale
    """
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0

    path = os.path.join("data", f"{crawler_id}.data")
    deadline = time.time() + 20  # wait up to 20 seconds

    while True:
        # Try in-memory job first (freshest state)
        with CrawlerJob._registry_lock:
            job = CrawlerJob.all_jobs.get(crawler_id)

        if job is not None:
            state = job.get_state()
            all_logs = state.get("logs", [])
            new_logs = all_logs[since:]
            if new_logs or state["status"] in ("done", "error"):
                return jsonify({
                    "crawler_id": crawler_id,
                    "status": state["status"],
                    "origin_url": state.get("origin_url", ""),
                    "max_depth": state.get("max_depth"),
                    "urls_processed": state["urls_processed"],
                    "urls_per_second": state.get("urls_per_second", 0),
                    "queue_depth": state["queue_depth"],
                    "max_queue_size": state.get("max_queue_size", 500),
                    "back_pressure_active": state["back_pressure_active"],
                    "new_logs": new_logs,
                    "log_index": len(all_logs),
                }), 200
        else:
            # Fall back to disk state (job from a previous session)
            if not os.path.exists(path):
                return jsonify({"error": "crawler not found"}), 404
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                all_logs = state.get("logs", [])
                new_logs = all_logs[since:]
                if new_logs or state.get("status") in ("done", "error"):
                    return jsonify({
                        "crawler_id": crawler_id,
                        "status": state.get("status", "unknown"),
                        "origin_url": state.get("origin_url", ""),
                        "max_depth": state.get("max_depth"),
                        "urls_processed": state.get("urls_processed", 0),
                        "urls_per_second": state.get("urls_per_second", 0),
                        "queue_depth": state.get("queue_depth", 0),
                        "max_queue_size": state.get("max_queue_size", 500),
                        "back_pressure_active": state.get("back_pressure_active", False),
                        "new_logs": new_logs,
                        "log_index": len(all_logs),
                    }), 200
            except (json.JSONDecodeError, OSError):
                return jsonify({"error": "could not read state file"}), 500

        # No new data yet — sleep and retry if deadline not reached
        if time.time() >= deadline:
            # Return current state with no new logs (client will re-poll)
            return jsonify({
                "crawler_id": crawler_id,
                "status": state.get("status", "unknown") if job is None else state["status"],
                "origin_url": state.get("origin_url", ""),
                "max_depth": state.get("max_depth"),
                "urls_processed": state.get("urls_processed", 0),
                "urls_per_second": state.get("urls_per_second", 0),
                "queue_depth": state.get("queue_depth", 0),
                "max_queue_size": state.get("max_queue_size", 500),
                "back_pressure_active": state.get("back_pressure_active", False),
                "new_logs": [],
                "log_index": since,
            }), 200

        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3600, debug=False, threaded=True)

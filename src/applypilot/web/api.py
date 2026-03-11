"""JSON API endpoints for the ApplyPilot web UI."""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request, Response

from applypilot.database import get_connection, get_stats
from applypilot.web.sse import bus, SSELogHandler
from applypilot.web import worker as pipeline_worker

api = Blueprint("api", __name__, url_prefix="/api")

# ---------------------------------------------------------------------------
# Scan state management
# ---------------------------------------------------------------------------

_CHECKPOINT_PATH = Path(os.path.expanduser("~/.applypilot/scan_checkpoint.json"))

_scan_state: dict = {
    "status": "idle",       # idle | running | stopping | stopped | done | error
    "phase": None,          # ats | jobspy | workday | enriching | scoring
    "progress_current": 0,
    "progress_total": 0,
    "progress_label": "",
    "started_at": None,
    "error": None,
    "has_checkpoint": _CHECKPOINT_PATH.exists(),
}
_scan_lock = threading.Lock()
_stop_event = threading.Event()
_scan_thread: threading.Thread | None = None


def _update_scan_state(**kwargs):
    """Thread-safe scan state update + publish SSE."""
    with _scan_lock:
        _scan_state.update(kwargs)
        snapshot = dict(_scan_state)
    bus.publish("scan_progress", snapshot)


def _load_checkpoint() -> dict | None:
    """Load checkpoint from disk, or None if not present."""
    if _CHECKPOINT_PATH.exists():
        try:
            return json.loads(_CHECKPOINT_PATH.read_text())
        except Exception:
            return None
    return None


def _save_checkpoint(data: dict):
    """Persist checkpoint to disk."""
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_PATH.write_text(json.dumps(data, indent=2))
    with _scan_lock:
        _scan_state["has_checkpoint"] = True


def _clear_checkpoint():
    """Remove checkpoint file."""
    if _CHECKPOINT_PATH.exists():
        _CHECKPOINT_PATH.unlink()
    with _scan_lock:
        _scan_state["has_checkpoint"] = False


@api.route("/jobs")
def list_jobs():
    """Paginated job list with filters."""
    conn = get_connection()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    per_page = min(per_page, 200)
    offset = (page - 1) * per_page

    # Filters
    min_score = request.args.get("min_score", type=int)
    max_score = request.args.get("max_score", type=int)
    remote_type = request.args.get("remote_type")
    company = request.args.get("company")
    title = request.args.get("title")
    country_code = request.args.get("country_code")
    site = request.args.get("site")
    selected = request.args.get("selected")
    pipeline_status = request.args.get("pipeline_status")
    search = request.args.get("search")
    user_status = request.args.get("user_status")
    company_tag = request.args.get("company_tag")
    hide_dismissed = request.args.get("hide_dismissed", "1")

    conditions = ["1=1"]
    params: list = []

    # Hide dismissed by default
    if hide_dismissed == "1":
        conditions.append("COALESCE(user_status, 'new') != 'dismissed'")

    if min_score is not None:
        conditions.append("fit_score >= ?")
        params.append(min_score)
    if max_score is not None:
        conditions.append("fit_score <= ?")
        params.append(max_score)
    if remote_type:
        conditions.append("remote_type = ?")
        params.append(remote_type)
    if company:
        conditions.append("company LIKE ?")
        params.append(f"%{company}%")
    if title:
        conditions.append("title LIKE ?")
        params.append(f"%{title}%")
    if country_code:
        conditions.append("country_code = ?")
        params.append(country_code)
    if site:
        conditions.append("site = ?")
        params.append(site)
    if selected == "1":
        conditions.append("ui_selected = 1")
    elif selected == "0":
        conditions.append("(ui_selected IS NULL OR ui_selected = 0)")
    if pipeline_status:
        conditions.append("pipeline_status = ?")
        params.append(pipeline_status)
    if user_status:
        conditions.append("COALESCE(user_status, 'new') = ?")
        params.append(user_status)
    if company_tag:
        conditions.append("company_tag = ?")
        params.append(company_tag)
    if search:
        conditions.append("(title LIKE ? OR company LIKE ? OR site LIKE ? OR brief_description LIKE ?)")
        pattern = f"%{search}%"
        params.extend([pattern, pattern, pattern, pattern])

    where = " AND ".join(conditions)

    # Count total matching
    count = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", params).fetchone()[0]

    # Fetch page
    rows = conn.execute(
        f"SELECT url, title, company, salary, salary_min, salary_max, salary_currency, "
        f"salary_period, location, site, remote_type, country_code, brief_description, "
        f"fit_score, score_reasoning, application_url, ui_selected, pipeline_status, "
        f"pipeline_error, tailored_resume_path, cover_letter_path, applied_at, apply_status, "
        f"user_status, user_viewed_at, first_seen_at, user_notes, company_tag "
        f"FROM jobs WHERE {where} "
        f"ORDER BY fit_score DESC NULLS LAST, discovered_at DESC "
        f"LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    jobs = []
    for row in rows:
        jobs.append(dict(zip(row.keys(), row)))

    return jsonify({
        "jobs": jobs,
        "total": count,
        "page": page,
        "per_page": per_page,
        "pages": (count + per_page - 1) // per_page if per_page else 1,
    })


@api.route("/jobs/<path:url>")
def job_detail(url):
    """Full job detail. Auto-sets user_viewed_at and transitions new → reviewing."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    job = dict(zip(row.keys(), row))

    # Auto-mark as viewed and transition new → reviewing
    now = datetime.now(timezone.utc).isoformat()
    if not job.get("user_viewed_at"):
        conn.execute(
            "UPDATE jobs SET user_viewed_at = ? WHERE url = ?",
            (now, url),
        )
        job["user_viewed_at"] = now
    if not job.get("user_status") or job["user_status"] == "new":
        conn.execute(
            "UPDATE jobs SET user_status = 'reviewing', user_status_at = ? "
            "WHERE url = ? AND COALESCE(user_status, 'new') = 'new'",
            (now, url),
        )
        job["user_status"] = "reviewing"
        job["user_status_at"] = now
    conn.commit()

    return jsonify(job)


@api.route("/stats")
def stats():
    """Pipeline statistics."""
    conn = get_connection()
    s = get_stats(conn)

    # Add UI-specific stats
    s["selected"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE ui_selected = 1"
    ).fetchone()[0]
    s["pipeline_queued"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'queued'"
    ).fetchone()[0]
    s["pipeline_done"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'ready_to_apply'"
    ).fetchone()[0]
    s["pipeline_failed"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE pipeline_status = 'failed'"
    ).fetchone()[0]
    s["worker_running"] = pipeline_worker.is_running()

    # Sites list for filter dropdown
    sites = conn.execute(
        "SELECT DISTINCT site FROM jobs WHERE site IS NOT NULL ORDER BY site"
    ).fetchall()
    s["sites_list"] = [r[0] for r in sites]

    # Country codes for filter
    countries = conn.execute(
        "SELECT DISTINCT country_code FROM jobs WHERE country_code IS NOT NULL ORDER BY country_code"
    ).fetchall()
    s["country_codes"] = [r[0] for r in countries]

    # Company tags for filter
    tags = conn.execute(
        "SELECT DISTINCT company_tag FROM jobs WHERE company_tag IS NOT NULL ORDER BY company_tag"
    ).fetchall()
    s["company_tags"] = [r[0] for r in tags]

    return jsonify(s)


@api.route("/jobs/select", methods=["POST"])
def select_jobs():
    """Mark jobs for the apply pipeline."""
    data = request.get_json()
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"error": "no urls provided"}), 400

    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for url in urls:
        result = conn.execute(
            "UPDATE jobs SET ui_selected = 1, ui_selected_at = ?, pipeline_status = 'queued' "
            "WHERE url = ? AND (ui_selected IS NULL OR ui_selected = 0)",
            (now, url),
        )
        count += result.rowcount
    conn.commit()

    for url in urls:
        bus.publish("job_status", {"url": url, "status": "queued"})

    return jsonify({"selected": count})


@api.route("/jobs/deselect", methods=["POST"])
def deselect_jobs():
    """Unmark jobs from the apply pipeline."""
    data = request.get_json()
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"error": "no urls provided"}), 400

    conn = get_connection()
    count = 0

    for url in urls:
        result = conn.execute(
            "UPDATE jobs SET ui_selected = 0, ui_selected_at = NULL, "
            "pipeline_status = NULL, pipeline_error = NULL "
            "WHERE url = ? AND ui_selected = 1 "
            "AND (pipeline_status IS NULL OR pipeline_status IN ('queued'))",
            (url,),
        )
        count += result.rowcount
    conn.commit()

    for url in urls:
        bus.publish("job_status", {"url": url, "status": None})

    return jsonify({"deselected": count})


@api.route("/pipeline/start", methods=["POST"])
def pipeline_start():
    """Start the background pipeline worker."""
    data = request.get_json() or {}
    min_score = data.get("min_score", 7)

    started = pipeline_worker.start_worker(min_score=min_score)
    if started:
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"})


@api.route("/pipeline/stop", methods=["POST"])
def pipeline_stop():
    """Stop the background pipeline worker."""
    stopped = pipeline_worker.stop_worker()
    return jsonify({"status": "stopped" if stopped else "not_running"})


@api.route("/pipeline/status")
def pipeline_status():
    """Worker + per-job pipeline status."""
    conn = get_connection()

    rows = conn.execute(
        "SELECT url, title, company, fit_score, pipeline_status, pipeline_error "
        "FROM jobs WHERE ui_selected = 1 "
        "ORDER BY pipeline_status DESC, fit_score DESC NULLS LAST"
    ).fetchall()

    jobs = [dict(zip(r.keys(), r)) for r in rows]

    return jsonify({
        "worker_running": pipeline_worker.is_running(),
        "jobs": jobs,
    })


@api.route("/scan/status")
def scan_status():
    """Return current scan state (used on page load to restore UI)."""
    with _scan_lock:
        state = dict(_scan_state)
    state["has_checkpoint"] = _CHECKPOINT_PATH.exists()
    return jsonify(state)


@api.route("/scan/stop", methods=["POST"])
def scan_stop():
    """Request graceful stop of running scan."""
    with _scan_lock:
        if _scan_state["status"] not in ("running",):
            return jsonify({"status": "not_running"})
        _scan_state["status"] = "stopping"
    _stop_event.set()
    bus.publish("scan_progress", {"status": "stopping"})
    return jsonify({"status": "stopping"})


@api.route("/discover", methods=["POST"])
def trigger_discover():
    """Trigger discover+enrich+score in background with progress tracking."""
    global _scan_thread

    resume = request.args.get("resume", "0") == "1"

    with _scan_lock:
        if _scan_state["status"] == "running":
            return jsonify({"status": "already_running"}), 409

    # Reset stop event for new run
    _stop_event.clear()

    # Load or clear checkpoint
    checkpoint = None
    if resume:
        checkpoint = _load_checkpoint()
    else:
        _clear_checkpoint()

    def _run_discovery():
        global _scan_thread
        from applypilot.database import _on_jobs_stored

        _update_scan_state(
            status="running",
            phase="ats",
            progress_current=0,
            progress_total=0,
            progress_label="Starting...",
            started_at=datetime.now(timezone.utc).isoformat(),
            error=None,
        )

        # Callback: publish SSE when new jobs are stored
        def _on_stored(new, existing, site, summary):
            bus.publish("jobs_discovered", {
                "new": new,
                "existing": existing,
                "site": site,
            })

        _on_jobs_stored.append(_on_stored)
        log_handler = SSELogHandler(bus)
        root_logger = logging.getLogger("applypilot")
        root_logger.addHandler(log_handler)

        cp = checkpoint or {}
        current_cp = {
            "ats_done": list(cp.get("ats_done", [])),
            "jobspy_done": list(cp.get("jobspy_done", [])),
            "workday_done": dict(cp.get("workday_done", {})),
            "phases_done": list(cp.get("phases_done", [])),
        }

        try:
            from applypilot.config import load_env, ensure_dirs
            from applypilot.database import init_db

            load_env()
            ensure_dirs()
            init_db()

            bus.publish("discover_status", {"status": "discovering"})

            # -- Progress callback for discovery sources --
            def _progress_cb(phase, current, total, label):
                _update_scan_state(
                    phase=phase,
                    progress_current=current,
                    progress_total=total,
                    progress_label=label,
                )

            from applypilot.pipeline import _run_discover, _run_enrich, _run_score

            # Run discovery with stop_event + checkpoint
            result = _run_discover(
                stop_event=_stop_event,
                progress_callback=_progress_cb,
                checkpoint=cp,
            )

            # Update checkpoint with completed items
            current_cp["ats_done"].extend(result.get("ats_completed_keys", []))
            current_cp["jobspy_done"].extend(result.get("jobspy_completed_indices", []))
            if result.get("workday_completed_queries"):
                current_cp["workday_done"].update(result["workday_completed_queries"])

            # Mark phases as done if not stopped
            if not _stop_event.is_set():
                for phase in ("ats", "jobspy", "workday"):
                    if phase not in current_cp["phases_done"]:
                        current_cp["phases_done"].append(phase)

            _save_checkpoint(current_cp)

            if _stop_event.is_set():
                _update_scan_state(status="stopped", phase=None, progress_label="Stopped")
                bus.publish("discover_status", {"status": "stopped"})
                return

            # -- Enrichment --
            _update_scan_state(phase="enriching", progress_current=0, progress_total=0, progress_label="Enriching...")
            bus.publish("discover_status", {"status": "enriching"})
            _run_enrich()

            if _stop_event.is_set():
                _update_scan_state(status="stopped", phase=None, progress_label="Stopped")
                bus.publish("discover_status", {"status": "stopped"})
                return

            # -- Scoring --
            _update_scan_state(phase="scoring", progress_current=0, progress_total=0, progress_label="Scoring...")
            bus.publish("discover_status", {"status": "scoring"})
            _run_score()

            # All done — clear checkpoint
            _clear_checkpoint()
            _update_scan_state(status="done", phase=None, progress_current=0, progress_total=0, progress_label="")
            bus.publish("discover_status", {"status": "done"})

        except Exception as e:
            _update_scan_state(status="error", error=str(e))
            bus.publish("discover_status", {"status": "error", "error": str(e)})
            # Save checkpoint on error so user can resume
            _save_checkpoint(current_cp)
        finally:
            try:
                _on_jobs_stored.remove(_on_stored)
            except ValueError:
                pass
            root_logger.removeHandler(log_handler)
            _scan_thread = None

    _scan_thread = threading.Thread(target=_run_discovery, name="discover-bg", daemon=True)
    _scan_thread.start()
    return jsonify({"status": "started", "resume": resume})


@api.route("/jobs/recent")
def recent_jobs():
    """Fetch recently discovered jobs, optionally after a timestamp."""
    conn = get_connection()
    limit = request.args.get("limit", 20, type=int)
    limit = min(limit, 100)
    after = request.args.get("after")

    if after:
        rows = conn.execute(
            "SELECT url, title, company, salary, salary_min, salary_max, salary_currency, "
            "salary_period, location, site, remote_type, country_code, brief_description, "
            "fit_score, score_reasoning, application_url, ui_selected, pipeline_status, "
            "user_status, user_viewed_at, first_seen_at, company_tag, discovered_at "
            "FROM jobs WHERE discovered_at > ? "
            "ORDER BY discovered_at DESC LIMIT ?",
            (after, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT url, title, company, salary, salary_min, salary_max, salary_currency, "
            "salary_period, location, site, remote_type, country_code, brief_description, "
            "fit_score, score_reasoning, application_url, ui_selected, pipeline_status, "
            "user_status, user_viewed_at, first_seen_at, company_tag, discovered_at "
            "FROM jobs ORDER BY discovered_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    jobs = [dict(zip(r.keys(), r)) for r in rows]
    return jsonify({"jobs": jobs})


# -- User status & notes endpoints ------------------------------------------

@api.route("/jobs/status", methods=["POST"])
def update_job_status():
    """Bulk update user_status for jobs."""
    data = request.get_json()
    urls = data.get("urls", [])
    status = data.get("status", "")
    if not urls or not status:
        return jsonify({"error": "urls and status required"}), 400

    valid_statuses = {"new", "reviewing", "shortlisted", "applied",
                      "interviewing", "offered", "rejected", "dismissed"}
    if status not in valid_statuses:
        return jsonify({"error": f"invalid status, must be one of: {valid_statuses}"}), 400

    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for url in urls:
        result = conn.execute(
            "UPDATE jobs SET user_status = ?, user_status_at = ? WHERE url = ?",
            (status, now, url),
        )
        count += result.rowcount
    conn.commit()

    for url in urls:
        bus.publish("job_status_change", {"url": url, "user_status": status})

    return jsonify({"updated": count})


@api.route("/jobs/notes", methods=["POST"])
def update_job_notes():
    """Save free-text notes for a job."""
    data = request.get_json()
    url = data.get("url", "")
    notes = data.get("notes", "")
    if not url:
        return jsonify({"error": "url required"}), 400

    conn = get_connection()
    conn.execute("UPDATE jobs SET user_notes = ? WHERE url = ?", (notes, url))
    conn.commit()

    return jsonify({"ok": True})


@api.route("/tracker")
def tracker_api():
    """Jobs grouped by user_status for Kanban view (excludes dismissed and new)."""
    conn = get_connection()

    columns = [
        "shortlisted", "applied", "interviewing", "offered", "rejected"
    ]
    result = {}

    for status in columns:
        rows = conn.execute(
            "SELECT url, title, company, fit_score, location, user_notes, "
            "user_status_at, application_url, company_tag "
            "FROM jobs WHERE user_status = ? "
            "ORDER BY user_status_at DESC",
            (status,),
        ).fetchall()
        result[status] = [dict(zip(r.keys(), r)) for r in rows]

    return jsonify(result)


# -- SSE endpoint -----------------------------------------------------------

@api.route("/events/stream")
def event_stream():
    """SSE stream for real-time updates."""
    client_queue = bus.subscribe()

    return Response(
        bus.stream(client_queue),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

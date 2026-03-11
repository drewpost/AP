"""Background pipeline worker for the web UI.

A daemon thread that processes user-selected jobs through
tailor -> cover letter -> (optional) auto-apply stages.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from applypilot.database import get_connection, init_db
from applypilot.web.sse import bus

log = logging.getLogger(__name__)

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _update_status(conn, url: str, status: str, error: str | None = None) -> None:
    """Update pipeline_status for a job and broadcast SSE event."""
    if error:
        conn.execute(
            "UPDATE jobs SET pipeline_status = ?, pipeline_error = ? WHERE url = ?",
            (status, error, url),
        )
    else:
        conn.execute(
            "UPDATE jobs SET pipeline_status = ?, pipeline_error = NULL WHERE url = ?",
            (status, url),
        )
    conn.commit()
    bus.publish("job_status", {"url": url, "status": status, "error": error})


def _process_job(conn, job: dict, min_score: int = 7) -> None:
    """Run tailor + cover letter for a single selected job."""
    url = job["url"]
    title = (job.get("title") or "")[:60]

    log.info("Processing: %s", title)
    bus.publish("job_status", {"url": url, "status": "tailoring", "title": title})

    # --- Tailoring ---
    if not job.get("tailored_resume_path"):
        _update_status(conn, url, "tailoring")
        try:
            from applypilot.config import load_profile, RESUME_PATH, TAILORED_DIR
            from applypilot.scoring.tailor import tailor_resume

            profile = load_profile()
            resume_text = RESUME_PATH.read_text(encoding="utf-8")
            resume_facts = profile.get("resume_facts", {})

            result = tailor_resume(resume_text, job, profile, resume_facts)

            if result.get("approved"):
                # Save tailored resume
                from pathlib import Path
                import re as _re
                safe_title = _re.sub(r"[^\w\s-]", "", job.get("title") or "job")[:50].strip()
                safe_site = _re.sub(r"[^\w\s-]", "", job.get("site") or "site")[:20].strip()
                filename = f"{safe_site}_{safe_title}".replace(" ", "_")
                txt_path = TAILORED_DIR / f"{filename}.txt"

                txt_path.write_text(result["resume"], encoding="utf-8")

                # Save job description alongside
                jd_path = TAILORED_DIR / f"{filename}_JOB.txt"
                jd_path.write_text(job.get("full_description", ""), encoding="utf-8")

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE jobs SET tailored_resume_path = ?, tailored_at = ?, "
                    "tailor_attempts = COALESCE(tailor_attempts, 0) + 1 WHERE url = ?",
                    (str(txt_path), now, url),
                )
                conn.commit()
                log.info("Tailored: %s", title)
            else:
                conn.execute(
                    "UPDATE jobs SET tailor_attempts = COALESCE(tailor_attempts, 0) + 1 WHERE url = ?",
                    (url,),
                )
                conn.commit()
                _update_status(conn, url, "failed", "Tailoring validation failed")
                return
        except Exception as e:
            log.error("Tailoring failed for %s: %s", title, e)
            _update_status(conn, url, "failed", f"Tailoring error: {e}")
            return
    else:
        log.info("Already tailored: %s", title)

    # --- Cover letter ---
    # Re-read to get updated tailored_resume_path
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        return
    job = dict(zip(row.keys(), row))

    if not job.get("cover_letter_path"):
        _update_status(conn, url, "covering")
        try:
            from applypilot.config import load_profile, RESUME_PATH, COVER_LETTER_DIR
            from applypilot.scoring.cover_letter import generate_cover_letter
            from pathlib import Path
            import re as _re

            profile = load_profile()
            resume_text = RESUME_PATH.read_text(encoding="utf-8")

            cl_text = generate_cover_letter(resume_text, job, profile)

            if cl_text:
                safe_title = _re.sub(r"[^\w\s-]", "", job.get("title") or "job")[:50].strip()
                safe_site = _re.sub(r"[^\w\s-]", "", job.get("site") or "site")[:20].strip()
                filename = f"{safe_site}_{safe_title}".replace(" ", "_")
                cl_path = COVER_LETTER_DIR / f"{filename}_CL.txt"
                cl_path.write_text(cl_text, encoding="utf-8")

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE jobs SET cover_letter_path = ?, cover_letter_at = ?, "
                    "cover_attempts = COALESCE(cover_attempts, 0) + 1 WHERE url = ?",
                    (str(cl_path), now, url),
                )
                conn.commit()
                log.info("Cover letter: %s", title)
            else:
                conn.execute(
                    "UPDATE jobs SET cover_attempts = COALESCE(cover_attempts, 0) + 1 WHERE url = ?",
                    (url,),
                )
                conn.commit()
        except Exception as e:
            log.error("Cover letter failed for %s: %s", title, e)
            # Non-fatal — job can still be applied manually

    _update_status(conn, url, "ready_to_apply")
    log.info("Ready to apply: %s", title)


def _worker_loop(min_score: int = 7) -> None:
    """Main worker loop — polls for selected jobs and processes them."""
    conn = init_db()

    try:
        while not _stop_event.is_set():
            # Find next queued job
            row = conn.execute(
                "SELECT * FROM jobs "
                "WHERE ui_selected = 1 "
                "AND (pipeline_status IS NULL OR pipeline_status = 'queued') "
                "AND full_description IS NOT NULL "
                "ORDER BY fit_score DESC NULLS LAST "
                "LIMIT 1"
            ).fetchone()

            if not row:
                # No work — wait and poll again
                if _stop_event.wait(timeout=5):
                    break
                continue

            job = dict(zip(row.keys(), row))
            _process_job(conn, job, min_score)

    except Exception as e:
        log.error("Worker crashed: %s", e)
        bus.publish("worker_status", {"status": "error", "error": str(e)})
    finally:
        conn.close()
        bus.publish("worker_status", {"status": "stopped"})


def start_worker(min_score: int = 7) -> bool:
    """Start the background pipeline worker. Returns True if started."""
    global _worker_thread, _stop_event

    if _worker_thread and _worker_thread.is_alive():
        return False  # Already running

    _stop_event = threading.Event()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        args=(min_score,),
        name="pipeline-worker",
        daemon=True,
    )
    _worker_thread.start()
    bus.publish("worker_status", {"status": "running"})
    return True


def stop_worker() -> bool:
    """Stop the background worker. Returns True if it was running."""
    global _worker_thread

    if not _worker_thread or not _worker_thread.is_alive():
        return False

    _stop_event.set()
    _worker_thread.join(timeout=30)
    _worker_thread = None
    return True


def is_running() -> bool:
    """Check if the worker is currently active."""
    return _worker_thread is not None and _worker_thread.is_alive()

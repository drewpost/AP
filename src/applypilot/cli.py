"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def lookup(
    query: str = typer.Argument(..., help="Search term: company name, job title, or keyword."),
    show: Optional[int] = typer.Option(None, "--show", "-s", help="Show full resume and cover letter for match number N."),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results to display."),
) -> None:
    """Search your applications by company, title, or keyword.

    Quick reference when a recruiter calls or emails — see what you sent them.

    Examples:
        applypilot lookup nvidia
        applypilot lookup "backend engineer"
        applypilot lookup nvidia --show 1
    """
    _bootstrap()

    from pathlib import Path
    from applypilot.database import search_jobs
    from rich.panel import Panel
    from rich.text import Text

    results = search_jobs(query, limit=limit)

    if not results:
        console.print(f"[yellow]No matches for[/yellow] \"{query}\"")
        raise typer.Exit()

    # If --show N, display full details for that match
    if show is not None:
        if show < 1 or show > len(results):
            console.print(f"[red]Invalid match number.[/red] Valid range: 1-{len(results)}")
            raise typer.Exit(code=1)

        job = results[show - 1]
        title = job.get("title") or "Unknown"
        site = job.get("site") or "Unknown"
        location = job.get("location") or ""
        score = job.get("fit_score")
        status = job.get("apply_status") or "not applied"
        applied_at = (job.get("applied_at") or "")[:10]
        url = job.get("application_url") or job.get("url") or ""
        score_reasoning = job.get("score_reasoning") or ""

        # Header
        console.print()
        console.print(Panel(
            f"[bold]{title}[/bold]\n"
            f"{site} · {location}\n"
            f"Score: [{'green' if score and score >= 7 else 'yellow'}]{score}/10[/{'green' if score and score >= 7 else 'yellow'}] · "
            f"Status: [bold]{status}[/bold]"
            + (f" · Applied: {applied_at}" if applied_at else "") + "\n"
            f"URL: {url}",
            title=f"Match #{show}",
            border_style="cyan",
        ))

        if score_reasoning:
            console.print(f"\n[bold]Score reasoning:[/bold] {score_reasoning}")

        # Show tailored resume
        resume_path = job.get("tailored_resume_path")
        if resume_path:
            txt_path = Path(resume_path)
            if not txt_path.exists():
                txt_path = txt_path.with_suffix(".txt")
            if txt_path.exists():
                resume_text = txt_path.read_text(encoding="utf-8")
                console.print()
                console.print(Panel(resume_text, title="Tailored Resume Sent", border_style="green"))
            else:
                console.print(f"\n[dim]Resume file not found: {resume_path}[/dim]")
        else:
            console.print("\n[dim]No tailored resume on file.[/dim]")

        # Show cover letter
        cl_path = job.get("cover_letter_path")
        if cl_path:
            cl_file = Path(cl_path)
            if not cl_file.exists():
                cl_file = cl_file.with_suffix(".txt")
            if cl_file.exists():
                cl_text = cl_file.read_text(encoding="utf-8")
                console.print()
                console.print(Panel(cl_text, title="Cover Letter Sent", border_style="blue"))
            else:
                console.print(f"\n[dim]Cover letter file not found: {cl_path}[/dim]")
        else:
            console.print("\n[dim]No cover letter on file.[/dim]")

        # Show job description if available
        job_desc_path = None
        if resume_path:
            job_desc_path = Path(resume_path).with_name(
                Path(resume_path).stem + "_JOB.txt"
            )
        if job_desc_path and job_desc_path.exists():
            jd_text = job_desc_path.read_text(encoding="utf-8")
            console.print()
            console.print(Panel(
                jd_text[:2000] + ("\n..." if len(jd_text) > 2000 else ""),
                title="Job Description",
                border_style="magenta",
            ))

        console.print()
        return

    # Show results table
    table = Table(
        title=f"Search results for \"{query}\" ({len(results)} match{'es' if len(results) != 1 else ''})",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Title / Company", min_width=30)
    table.add_column("Score", justify="center", width=5)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Applied", justify="center", width=10)

    for i, job in enumerate(results, 1):
        title = (job.get("title") or "Unknown")[:50]
        site = (job.get("site") or "")[:25]
        location = (job.get("location") or "")[:25]
        score = job.get("fit_score")
        status = job.get("apply_status") or "-"
        applied_at = (job.get("applied_at") or "")[:10]

        score_str = f"{score}/10" if score else "-"
        if score and score >= 7:
            score_str = f"[green]{score_str}[/green]"
        elif score and score >= 5:
            score_str = f"[yellow]{score_str}[/yellow]"

        if status == "applied":
            status = f"[green]{status}[/green]"
        elif status == "failed":
            status = f"[red]{status}[/red]"

        subtitle = f"[dim]{site}[/dim]"
        if location:
            subtitle += f" [dim]· {location}[/dim]"

        table.add_row(str(i), f"{title}\n{subtitle}", score_str, status, applied_at or "-")

    console.print()
    console.print(table)
    console.print(f"\n[dim]Use [bold]applypilot lookup \"{query}\" --show N[/bold] to view the resume and cover letter for match N.[/dim]")
    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def web(
    port: int = typer.Option(5000, "--port", "-p", help="Port to run the web server on."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to."),
    debug: bool = typer.Option(False, "--debug", help="Enable Flask debug mode."),
) -> None:
    """Launch the local web UI for browsing and selecting jobs."""
    _bootstrap()

    from applypilot.web.app import create_app

    flask_app = create_app()

    console.print(f"\n[bold blue]ApplyPilot Web UI[/bold blue]")
    console.print(f"  http://{host}:{port}")
    console.print(f"  Press Ctrl+C to stop.\n")

    flask_app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    app()

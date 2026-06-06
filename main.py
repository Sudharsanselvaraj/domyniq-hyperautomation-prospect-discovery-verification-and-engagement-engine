"""
main.py — Cold Outreach Pipeline Entry Point

This is the single CLI command that drives the entire 4-stage pipeline.
Architecture note: we use Typer for ergonomic CLI definition and Rich for
terminal output. The actual business logic lives in services/ — main.py
is only responsible for wiring those services together and surfacing results
to the operator.

Interview talking point:
  "main.py is intentionally thin — it's the composition root.
   Every real decision happens in a service, which keeps this file
   readable and the services unit-testable in isolation."
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from config.settings import Settings
from services.orchestrator import PipelineOrchestrator
from utils.logger import get_logger
from utils.metrics import PipelineMetrics
from utils.resume import ResumableRun

app = typer.Typer(
    name="outreach",
    help="Automated cold-outreach pipeline: one domain → emails sent.",
    add_completion=False,
)
console = Console()
logger = get_logger(__name__)


def _print_banner() -> None:
    banner = Text()
    banner.append("⚡ COLD OUTREACH PIPELINE", style="bold cyan")
    banner.append(" v1.0.0\n", style="dim")
    banner.append("Vocallabs SDE Assignment — Production Build", style="dim italic")
    console.print(Panel(banner, border_style="cyan", padding=(1, 4)))


def _print_stage_table(metrics: PipelineMetrics) -> None:
    table = Table(
        title="Pipeline Execution Summary",
        show_header=True,
        header_style="bold magenta",
        border_style="magenta",
        show_lines=True,
    )
    table.add_column("Stage", style="cyan", no_wrap=True)
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Status", justify="center")

    for stage in metrics.stages:
        status_icon = "✅" if stage.success else "❌"
        table.add_row(
            stage.name,
            str(stage.input_count),
            str(stage.output_count),
            f"{stage.duration_seconds:.1f}s",
            status_icon,
        )
    console.print(table)


def _safety_checkpoint(
    companies: int, contacts: int, verified: int
) -> bool:
    """
    Safety checkpoint — show a summary before emails actually fire.
    This is the one human gate in an otherwise fully-automated pipeline.
    Interview talking point: "I never fire emails blindly. One confirmation
    prevents us from spamming half the internet during a dev test."
    """
    console.print()
    console.print(
        Panel.fit(
            f"[bold white]PIPELINE SUMMARY[/bold white]\n\n"
            f"  Companies Found    : [cyan]{companies}[/cyan]\n"
            f"  Contacts Found     : [cyan]{contacts}[/cyan]\n"
            f"  Verified Emails    : [cyan]{verified}[/cyan]\n\n"
            f"  [bold green]Emails Ready To Send : {verified}[/bold green]",
            title="[bold yellow]⚠  REVIEW BEFORE SEND[/bold yellow]",
            border_style="yellow",
            padding=(1, 4),
        )
    )
    console.print()
    answer = typer.prompt(
        "Proceed and send all emails? [y/n]",
        default="n",
    )
    return answer.strip().lower() == "y"


@app.command()
def run(
    domain: str = typer.Argument(
        ...,
        help="Seed company domain, e.g. openai.com",
        metavar="DOMAIN",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-d",
        help="Run all stages but skip the final email send.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        "-r",
        help="Resume from last checkpoint if a previous run exists for this domain.",
    ),
    max_companies: int = typer.Option(
        25,
        "--max-companies",
        "-n",
        help="Maximum number of similar companies to fetch from Ocean.io.",
    ),
    output_json: bool = typer.Option(
        False,
        "--json-output",
        help="Also write results to data/output.json in addition to CSV.",
    ),
) -> None:
    """
    Run the full 4-stage cold outreach pipeline for a single seed domain.

    \b
    Stages:
      [1/4] Ocean.io   — find lookalike companies
      [2/4] Prospeo    — extract decision-makers + LinkedIn URLs
      [3/4] EazyReach  — resolve verified work emails
      [4/4] Brevo      — send personalised outreach emails

    Example:
      python main.py openai.com
      python main.py stripe.com --dry-run
      python main.py notion.so --resume --max-companies 50
    """
    _print_banner()
    logger.info("Pipeline started", extra={"domain": domain, "dry_run": dry_run})
    start_time = datetime.utcnow()

    # ── Config ──────────────────────────────────────────────────────────────
    try:
        settings = Settings()
    except Exception as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        console.print("Ensure your .env file is populated. See README.md.")
        raise typer.Exit(code=1)

    # ── Resume support ───────────────────────────────────────────────────────
    resumable = ResumableRun(domain)
    if resume and resumable.has_checkpoint():
        console.print(
            f"[bold yellow]↩  Resuming previous run for[/bold yellow] [cyan]{domain}[/cyan]"
        )
    else:
        resumable.clear()

    # ── Progress bar setup ───────────────────────────────────────────────────
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    orchestrator = PipelineOrchestrator(
        settings=settings,
        progress=progress,
        resumable=resumable,
        max_companies=max_companies,
    )

    # ── Execute pipeline ─────────────────────────────────────────────────────
    with progress:
        result = asyncio.run(
            orchestrator.execute(seed_domain=domain)
        )

    # ── Metrics display ──────────────────────────────────────────────────────
    console.print()
    _print_stage_table(result.metrics)
    console.print()

    if not result.leads:
        console.print("[bold red]No leads found — nothing to send.[/bold red]")
        raise typer.Exit(code=0)

    # ── Safety checkpoint ────────────────────────────────────────────────────
    if dry_run:
        console.print(
            "[bold yellow]--dry-run active:[/bold yellow] "
            "skipping email send. CSV written regardless."
        )
        confirmed = False
    else:
        confirmed = _safety_checkpoint(
            companies=result.metrics.companies_found,
            contacts=result.metrics.contacts_found,
            verified=result.metrics.verified_emails,
        )

    # ── Stage 4: Send emails ──────────────────────────────────────────────────
    if confirmed:
        console.print("[bold green]Sending emails…[/bold green]")
        send_result = asyncio.run(
            orchestrator.send_emails(leads=result.leads)
        )
        result.metrics.emails_sent = send_result.sent
        result.metrics.emails_failed = send_result.failed
        logger.info(
            "Emails sent",
            extra={"sent": send_result.sent, "failed": send_result.failed},
        )
    else:
        console.print("[dim]Email send skipped.[/dim]")

    # ── Write outputs ─────────────────────────────────────────────────────────
    orchestrator.write_csv(result.leads, Path("data/output.csv"))
    if output_json:
        orchestrator.write_json(result.leads, Path("data/output.json"))

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed = (datetime.utcnow() - start_time).total_seconds()
    console.print()
    console.print(
        Panel.fit(
            f"[bold green]✓  Pipeline complete[/bold green]  "
            f"[dim]({elapsed:.1f}s total)[/dim]\n\n"
            f"  Emails sent    : [green]{result.metrics.emails_sent}[/green]\n"
            f"  Emails failed  : [red]{result.metrics.emails_failed}[/red]\n"
            f"  Report         : [cyan]data/output.csv[/cyan]"
            + (f"\n  JSON export    : [cyan]data/output.json[/cyan]" if output_json else ""),
            border_style="green",
        )
    )
    logger.info(
        "Pipeline finished",
        extra={
            "domain": domain,
            "elapsed_seconds": elapsed,
            "emails_sent": result.metrics.emails_sent,
        },
    )
    resumable.clear()  # clean up checkpoint on successful completion


if __name__ == "__main__":
    app()

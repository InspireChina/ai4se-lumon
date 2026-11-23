"""Run Auto Test through the same service used by the Dashboard."""

from __future__ import annotations

import asyncio
import signal
from typing import Annotated
from uuid import UUID

import typer

from lumon.auto_test.model import PollResult
from lumon.auto_test.service import AutoTestService
from lumon.errors import LumonError

test_app = typer.Typer(
    name="test", help="Run Jira acceptance tests.", no_args_is_help=True, add_completion=False
)
WorkspaceId = Annotated[UUID, typer.Option("--workspace-id")]


@test_app.command("poll")
def poll(
    workspace_id: WorkspaceId,
    manual: Annotated[
        bool, typer.Option("--manual", help="Check Jira once even with scheduling off.")
    ] = False,
    retest: Annotated[UUID | None, typer.Option("--retest")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check the saved JQL and execute one eligible issue."""
    try:
        result = asyncio.run(_poll(workspace_id, manual or retest is not None, retest))
    except asyncio.CancelledError as exc:
        raise typer.Exit(code=130) from exc
    except LumonError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    if json_output:
        typer.echo(result.model_dump_json())
    else:
        typer.echo(f"Auto Test: {result.status}" + (f" · {result.detail}" if result.detail else ""))
    if result.status == "failed":
        raise typer.Exit(code=1)


async def _poll(workspace_id: UUID, manual: bool, retest: UUID | None) -> PollResult:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(AutoTestService().poll(workspace_id, manual=manual, retest=retest))
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await task
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


@test_app.command("stop")
def stop(workspace_id: WorkspaceId, run_id: UUID) -> None:
    """Request cancellation of the selected run."""
    try:
        AutoTestService().store(workspace_id).stop(run_id)
    except LumonError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    typer.echo("Auto Test stopping.")


@test_app.command("publish")
def publish(workspace_id: WorkspaceId, run_id: UUID) -> None:
    """Retry publishing stored evidence and the existing Jira result comment."""
    try:
        run = asyncio.run(AutoTestService().retry_publication(workspace_id, run_id))
    except LumonError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    typer.echo(run.jira.model_dump_json())
    if run.jira.status != "published":
        raise typer.Exit(code=1)

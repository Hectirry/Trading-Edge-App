"""Backtest job worker — spawns a child process per job, captures stdout/
stderr, updates `research.backtest_jobs`, and records the resulting
backtest_id when the run completes."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import UTC, datetime

from trading.api.db import get_job, update_job
from trading.common.logging import get_logger

log = get_logger("api.worker")

TIMEOUT_S = 15 * 60  # 15 minutes hard cap


def _run_backtest_subprocess(job: dict, job_id: str) -> dict:
    cmd = [
        "python",
        "-m",
        "trading.cli.backtest",
        "--strategy",
        job["strategy_name"],
        "--params",
        job["params_file"],
        "--from",
        job["from_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--to",
        job["to_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--source",
        job["data_source"],
    ]
    if job.get("polybot_db_path"):
        cmd += ["--polybot-db", job["polybot_db_path"]]
    if job.get("slug_encodes_open_ts"):
        cmd += ["--slug-encodes-open-ts"]

    proc = subprocess.Popen(
        cmd,
        cwd="/app",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ},
    )
    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_S)
        exit_code = proc.returncode
        status = "completed" if exit_code == 0 else "failed"
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        exit_code = -9
        status = "timeout"
    return {
        "stdout": stdout[-10000:] if stdout else "",
        "stderr": stderr[-10000:] if stderr else "",
        "exit_code": exit_code,
        "status": status,
    }


async def run_job(job_id: str) -> None:
    job = await get_job(job_id)
    if job is None:
        log.error("worker.job_not_found", job_id=job_id)
        return

    await update_job(job_id, status="running", started_at=datetime.now(tz=UTC))
    log.info("worker.job.start", job_id=job_id, strategy=job["strategy_name"])

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run_backtest_subprocess, job, job_id)

    backtest_id = _extract_backtest_id(result["stdout"])
    await update_job(
        job_id,
        status=result["status"],
        finished_at=datetime.now(tz=UTC),
        exit_code=result["exit_code"],
        stdout_tail=result["stdout"],
        stderr_tail=result["stderr"],
        backtest_id=backtest_id,
    )
    log.info(
        "worker.job.done",
        job_id=job_id,
        status=result["status"],
        backtest_id=backtest_id,
    )


_BID_RE = re.compile(r'"backtest_id": "([0-9a-f-]+)"')


def _extract_backtest_id(stdout: str) -> str | None:
    if not stdout:
        return None
    m = _BID_RE.search(stdout)
    return m.group(1) if m else None

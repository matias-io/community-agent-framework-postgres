"""Every sample that needs only PostgreSQL runs, in its own process, and prints what its docstring promises."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


@pytest.fixture
def sample_env(test_dsn: str, schema: str) -> dict[str, str]:
    return os.environ | {"POSTGRES_CONNECTION_STRING": test_dsn, "SAMPLE_SCHEMA": schema}


def _run(env: dict[str, str], script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SAMPLES / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("history_provider.py", "stored: ['hi', 'hello', 'thanks']"),
        ("session_store.py", "restored: conversation-1"),
        ("checkpoint_storage.py", "output: ['HELLO!']"),
        ("thread_snapshot_store.py", "user-2: None"),
        ("leases.py", "held by: worker-2"),
    ],
)
async def test_the_sample_runs(sample_env: dict[str, str], script: str, expected: str) -> None:
    result = await asyncio.to_thread(_run, sample_env, script)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


async def test_a_workflow_killed_halfway_finishes_in_a_new_process(sample_env: dict[str, str]) -> None:
    crashed = await asyncio.to_thread(_run, sample_env, "resume_after_restart.py", "crash")
    assert crashed.returncode == 1, crashed.stderr
    assert "step two: the process dies" in crashed.stdout
    resumed = await asyncio.to_thread(_run, sample_env, "resume_after_restart.py", "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert "output: ['HELLO!']" in resumed.stdout
    # The sample deletes its checkpoints once the run finishes.
    again = await asyncio.to_thread(_run, sample_env, "resume_after_restart.py", "resume")
    assert again.returncode == 0, again.stderr
    assert "nothing to resume" in again.stdout

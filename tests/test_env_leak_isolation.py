"""The suite must never reach the developer's real memory store.

On 2026-09-13 a machine-level ``LEVH_SQLITE_DB_PATH`` turned 28 subprocess
tests into writers of the real database: ``server.core.env.get_env`` prefers
the ``LEVH_``-prefixed name over the plain ``SQLITE_DB_PATH`` the suite sets,
and CI — where the variable does not exist — stayed green. Fourteen fixture
memories, five fake guard violations, fourteen attachments and the whole
entity graph landed in production memory before anyone noticed.

The scrub lives in ``tests/conftest.py``. These tests pin its contract: the
suite's own environment starts free of every variable that can steer a
subprocess at a real store, and a subprocess actually resolves the store the
suite points it at. On a hostile machine (the CI ``hostile-env`` job plants
one) the first test fails before the fix and passes after it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# Must match tests/conftest.py. get_env() accepts plain, LEVH_-prefixed and
# legacy STACKMEMORY_ spellings, and LEVH_CONFIG_PATH redirects config
# resolution — all four can end with the suite reading someone's real data.
STEERING_NAMES = (
    "LEVH_SQLITE_DB_PATH",
    "SQLITE_DB_PATH",
    "STACKMEMORY_SQLITE_DB_PATH",
    "LEVH_CONFIG_PATH",
)


def test_the_suite_starts_with_no_db_steering_variable():
    leaked = [name for name in STEERING_NAMES if name in os.environ]
    assert leaked == [], (
        "the suite inherited store-steering variables "
        f"{leaked}; tests/conftest.py scrub failed and subprocess tests "
        "may read or write the developer's real memory"
    )


def test_a_subprocess_resolves_the_store_the_suite_points_at(tmp_path):
    db = tmp_path / "isolated.db"
    env = {k: v for k, v in os.environ.items() if k not in STEERING_NAMES}
    env["SQLITE_DB_PATH"] = str(db)
    env["EMBEDDER_MODE"] = "hash"

    first = subprocess.run(
        [sys.executable, "-m", "server.cli", "setup", "--real", "--client", "claude", "--profile", "work"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert first.returncode == 0, first.stderr

    status = subprocess.run(
        [sys.executable, "-m", "server.cli", "setup", "--status"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert status.returncode == 0, status.stderr
    data = json.loads(status.stdout)
    assert data["memory_count"] == 0, (
        "a subprocess resolved a store other than the one the suite pointed "
        f"it at ({db}); this is how the 2026-09-13 leak happened"
    )
    assert db.exists(), "the isolated database was never created"

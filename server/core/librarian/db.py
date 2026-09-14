"""Read-only database access for the librarian.

Split out of the single-file librarian (issue #98): the engine's real db
path resolution and a read-only connection helper live here. Librarian is
a watcher — everything it does against the store is read-only; writes go
through the engine via findings.
"""

from __future__ import annotations

import logging
import os
import sqlite3

logger = logging.getLogger("levh.librarian")


def db_path() -> str:
    """The database the ENGINE uses — not a guess at where it might live.

    Reading a different file than the engine writes to made the activity
    report describe an empty database and call every agent silent.
    """
    try:
        from server.core import engine_provider

        path = engine_provider.get_engine().db.db_path
        if path:
            return str(path)
    except Exception:  # noqa: BLE001 — motor yoksa (CLI) yapılandırmaya düş
        logger.debug("librarian could not read the engine db path")
    try:
        from server.core.runtime_config import resolve_runtime_config

        return resolve_runtime_config().database_path
    except Exception:  # noqa: BLE001 — config bozuksa rapor yine de çıksın
        return os.getenv(
            "SQLITE_DB_PATH",
            os.path.join(os.path.expanduser("~"), "AppData", "Local", "stackmemory.db"),
        )


def ro_conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True, timeout=5)

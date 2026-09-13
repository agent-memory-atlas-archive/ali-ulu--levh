"""Shared test setup.

The suite must describe its own environment. It did not, in two ways.

Several tests assert what happens with *no* LLM configuration but read the
developer's real environment, so on a machine with OPENAI_BASE_URL pointed at
OpenRouter and SUMMARY_MODE set they failed — while passing in CI and on a
clean checkout. That is the worst failure mode a test can have: red for a
reason that has nothing to do with the change under test, which trains
everyone to ignore it. Clearing those variables makes "unset" the baseline; a
test that wants a value still sets it with monkeypatch, which runs after this
fixture and wins.

The librarian watcher is the other one. It starts with the app and writes into
whatever store the app is using — a row no test asked for, arriving from a
background thread at an unpredictable moment. Tests that exercise the watcher
turn it on themselves.

It happened a third way, on 2026-09-13: the developer's machine exported
``LEVH_SQLITE_DB_PATH`` pointing at the real memory store, and
``server.core.env.get_env`` prefers the ``LEVH_``-prefixed name over the plain
one the suite sets. Subprocess-based tests therefore ignored their own
``SQLITE_DB_PATH`` and wrote fixture rows into the *real* memory. CI never saw
it because Actions machines have no such variable — the failure was invisible
exactly where the suite is trusted most. The suite now scrubs every name that
can steer it at a real store, and asserts the decoy survives the whole run:
if a test still reaches the real database, the canary row proves it and this
file is the first suspect.
"""

from __future__ import annotations

import os

import pytest

os.environ["LEVH_LIBRARIAN"] = "0"

# Read by server.core.llm_endpoint, llm_policy and summarizer. Anything here
# changes whether a call goes out, where it goes, and with which model.
_LLM_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "SUMMARY_MODE",
    "SUMMARY_MODEL",
)

# Every name through which a developer's environment could redirect the suite
# (or the CLI/server/MCP subprocesses it spawns) at the real memory store.
# get_env() accepts plain, LEVH_-prefixed and legacy STACKMEMORY_ spellings,
# so all three must go.
_DB_ENV = (
    "LEVH_SQLITE_DB_PATH",
    "SQLITE_DB_PATH",
    "STACKMEMORY_SQLITE_DB_PATH",
    "LEVH_CONFIG_PATH",
)


@pytest.fixture(autouse=True)
def _neutral_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LLM_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_memory_store(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _DB_ENV:
        monkeypatch.delenv(name, raising=False)


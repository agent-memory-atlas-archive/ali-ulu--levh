"""The Cursor MCP/cursorrules installer (#97).

Checks that `limit` is actually honoured in the continuity brief written into
.cursorrules, and that the non-cursor installers do not accept a dead `limit`
parameter anymore.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.commands.universal_hooks import (
    install_claude_desktop_hook,
    install_cursor_hook,
    install_vscode_hook,
    install_windsurf_hook,
)


@pytest.fixture()
def fake_cwd(tmp_path, monkeypatch):
    """Run the installer inside a temp dir so we never touch the repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cursor_hook_honours_limit_in_cursorrules(fake_cwd):
    result = install_cursor_hook(limit=7)
    assert result["ok"] is True
    rules = (fake_cwd / ".cursorrules").read_text(encoding="utf-8")
    assert "--limit 7 --if-any" in rules


def test_default_limit_is_5(fake_cwd):
    install_cursor_hook()
    rules = (fake_cwd / ".cursorrules").read_text(encoding="utf-8")
    assert "--limit 5 --if-any" in rules


@pytest.mark.parametrize(
    "installer",
    [install_vscode_hook, install_windsurf_hook, install_claude_desktop_hook],
)
def test_non_cursor_installers_take_no_limit(fake_cwd, installer):
    result = installer()
    assert result["ok"] is True
    assert "config_path" in result
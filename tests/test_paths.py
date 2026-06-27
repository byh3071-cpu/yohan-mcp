# -*- coding: utf-8 -*-
from pathlib import Path

from core.paths import resolve_memory_dir, resolve_mcp_runtime_dir


def test_resolve_memory_dir_from_brain_root(monkeypatch, tmp_path):
    brain = tmp_path / "yohan-brain"
    (brain / "memory").mkdir(parents=True)
    monkeypatch.delenv("MEMORY_DIR", raising=False)
    monkeypatch.setenv("YOHAN_BRAIN_ROOT", str(brain))
    assert resolve_memory_dir() == brain / "memory"


def test_resolve_mcp_runtime_dir_brain_linked(monkeypatch, tmp_path):
    brain = tmp_path / "yohan-brain"
    (brain / "memory").mkdir(parents=True)
    monkeypatch.delenv("MEMORY_DIR", raising=False)
    monkeypatch.delenv("MCP_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("YOHAN_BRAIN_ROOT", str(brain))
    assert resolve_mcp_runtime_dir() == brain / "memory" / "ops" / "mcp-runtime"

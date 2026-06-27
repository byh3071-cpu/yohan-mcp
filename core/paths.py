# -*- coding: utf-8 -*-
"""Path resolution — yohan-brain memory SoT vs MCP runtime journals (ADR-008 E4-01)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def resolve_memory_dir() -> Path:
    """Knowledge SoT: prefer yohan-brain/memory via YOHAN_BRAIN_ROOT or MEMORY_DIR."""
    if md := os.getenv("MEMORY_DIR"):
        return Path(md)
    if brain := os.getenv("YOHAN_BRAIN_ROOT"):
        return Path(brain) / "memory"
    return ROOT / "memory"


def resolve_mcp_runtime_dir() -> Path:
    """Operational JSONL journals — isolated under ops/mcp-runtime when brain-linked."""
    if rd := os.getenv("MCP_RUNTIME_DIR"):
        return Path(rd)
    mem = resolve_memory_dir()
    local_default = (ROOT / "memory").resolve()
    if mem.resolve() == local_default:
        return mem
    return mem / "ops" / "mcp-runtime"

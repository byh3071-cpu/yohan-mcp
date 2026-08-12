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


def resolve_knowledge_runtime_dir() -> Path:
    """Private knowledge worker state.

    Unlike MCP journals this directory must never fall inside yohan-brain,
    because NotebookLM indexes and review staging are operational caches, not
    knowledge SoT. ``data/`` is ignored by this repository.
    """
    if runtime := os.getenv("KNOWLEDGE_RUNTIME_DIR"):
        resolved = Path(runtime).expanduser().resolve()
    else:
        resolved = (ROOT / "data" / "knowledge").resolve()

    forbidden_roots: list[Path] = []
    if brain := os.getenv("YOHAN_BRAIN_ROOT"):
        forbidden_roots.append(Path(brain).expanduser().resolve())
    if memory := os.getenv("MEMORY_DIR"):
        forbidden_roots.append(Path(memory).expanduser().resolve())

    for forbidden in forbidden_roots:
        try:
            resolved.relative_to(forbidden)
        except ValueError:
            continue
        raise RuntimeError(
            "KNOWLEDGE_RUNTIME_DIR must stay outside YOHAN_BRAIN_ROOT and MEMORY_DIR"
        )
    return resolved

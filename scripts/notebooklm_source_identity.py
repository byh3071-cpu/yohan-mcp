"""Emit only safe NotebookLM type-9 source identities for one notebook.

Raw NotebookLM RPC metadata, titles, and errors remain in this short-lived
process.  stdout is deliberately limited to source IDs and canonical YouTube
URLs so the parent process cannot persist or display the raw response.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.knowledge import extract_youtube_source_identities  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        return 2
    logging.disable(logging.CRITICAL)
    try:
        from notebooklm_tools.cli.utils import get_client

        # `get_client` owns the current NotebookLM authentication context.
        # Keeping it scoped to this helper prevents that context (and any
        # associated diagnostics) from crossing the minimal-output boundary.
        with get_client() as client:
            raw = client.get_notebook(argv[1])
        identities = extract_youtube_source_identities(raw)
        json.dump(
            {"sources": [{"source_id": key, "url": value} for key, value in sorted(identities.items())]},
            sys.stdout,
            separators=(",", ":"),
        )
        return 0
    except Exception:
        # The caller maps this to a fixed code/message and deliberately never
        # consumes stderr, preventing cookies/RPC details from escaping.
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

# -*- coding: utf-8 -*-
"""테스트 격리 — 각 테스트마다 런타임/메모리 경로를 고유 tmp 로 분리.

E4-01(5e719a9)에서 ToolContext 가 어댑터 base 기반 격리를 버리고
resolve_mcp_runtime_dir()(전역 env 경로)로 전환하면서, 테스트들이 같은
JSONL 저널(approvals/runs/created/links/policy/trigger_runs)을 공유해
누적 상태 오염으로 실패하게 됨(단독 실행은 통과). 각 테스트를 고유
tmp_path 로 격리해 그 회귀를 복원한다. 실제 인프라(Qdrant/brain) 의존도
함께 차단한다.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.delenv("YOHAN_BRAIN_ROOT", raising=False)
    monkeypatch.delenv("QDRANT_URL", raising=False)

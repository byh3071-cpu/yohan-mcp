# -*- coding: utf-8 -*-
"""scripts/eval_recall.py — 골든셋 로더 + 경로 정규화 + 골든셋 파일 자체의 정합성.

검색 품질 측정은 실 Qdrant·ollama 가 필요해 이 스위트 밖(수동)에서 한다. 여기서는
측정 도구가 조용히 망가지는 경로만 막는다 — 특히 골든셋의 expect 경로가 실재하지
않으면 그 문항은 영원히 실패로 나오는데, 검색이 나빠진 건지 문서가 이름만 바뀐
건지 구분이 안 된다.
"""

import os
from pathlib import Path

import pytest
import yaml

from core.paths import ROOT
from scripts.eval_recall import DEFAULT_GOLDEN_REL, _payload_path_to_repo, load_golden


def _write(tmp_path, data):
    p = tmp_path / "golden.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_payload_path_normalized_to_repo_root():
    """payload 는 memory/ 기준, 골든셋은 레포루트 기준 — 안 맞추면 전부 실패로 나온다."""
    assert _payload_path_to_repo("wiki/concepts/x.md") == "memory/wiki/concepts/x.md"
    assert _payload_path_to_repo("rules\\y.md") == "memory/rules/y.md"


def test_load_golden_ok(tmp_path):
    p = _write(
        tmp_path,
        {
            "version": 1,
            "questions": [{"id": "q1", "question": "물음", "expect": ["a.md"]}],
        },
    )
    assert len(load_golden(p)["questions"]) == 1


def test_load_golden_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        load_golden(_write(tmp_path, {"version": 1, "questions": []}))


@pytest.mark.parametrize("missing", ["id", "question", "expect"])
def test_load_golden_rejects_incomplete_item(tmp_path, missing):
    """항목이 반쪽이면 즉시 터진다 — 조용히 0점을 내는 것보다 낫다."""
    item = {"id": "q1", "question": "물음", "expect": ["a.md"]}
    del item[missing]
    with pytest.raises(ValueError):
        load_golden(_write(tmp_path, {"version": 1, "questions": [item]}))


# ── 실 골든셋 파일 정합성 (brain 레포가 옆에 있을 때만) ──
def _real_golden():
    """실 골든셋 경로. resolve_memory_dir() 를 쓰지 않는다.

    conftest 의 autouse fixture 가 MEMORY_DIR 을 tmp_path 로 덮어써서, 그걸 타면 이
    테스트들이 **영구 스킵**된다(테스트가 있는 척만 하는 상태). brain 레포 위치를
    직접 계산하고, 워크트리 등 다른 체크아웃을 검증할 때만 GOLDEN_SET_PATH 로 덮어쓴다.
    """
    override = os.getenv("GOLDEN_SET_PATH")
    path = (
        Path(override)
        if override
        else ROOT.parent / "yohan-brain" / "memory" / DEFAULT_GOLDEN_REL
    )
    if not path.exists():
        pytest.skip(f"골든셋 없음(brain 레포 미연결): {path}")
    return path, load_golden(path)


def test_real_golden_ids_unique():
    _, data = _real_golden()
    ids = [q["id"] for q in data["questions"]]
    assert len(ids) == len(set(ids)), f"중복 id: {[i for i in ids if ids.count(i) > 1]}"


def test_real_golden_expect_paths_exist():
    """expect 경로는 brain 레포 루트 기준 실파일이어야 한다."""
    path, data = _real_golden()
    repo_root = path.parents[3]  # <root>/memory/wiki/answers/golden-set.yaml
    missing = [
        (q["id"], e)
        for q in data["questions"]
        for e in q["expect"]
        if not (repo_root / e).exists()
    ]
    assert not missing, f"실재하지 않는 expect 경로: {missing}"


def test_real_golden_has_unindexed_control_group():
    """대조군이 사라지면 allowlist 확장 효과를 잴 방법이 없어진다."""
    _, data = _real_golden()
    control = [q for q in data["questions"] if not q.get("indexed", True)]
    assert control, "indexed: false 대조군 문항이 하나도 없다"

# -*- coding: utf-8 -*-
"""Verifiability Engine — 6항목 품질/룰북/크로스링크/모순/봉투 (P3)."""
import json

from core import verify as V
from core.schema_validator import SchemaValidator

VALIDATOR = SchemaValidator()
LINKS = json.loads((VALIDATOR.dir / "_links.json").read_text(encoding="utf-8"))["links"]

VALID_SUMMARY = {
    "summary_id": "sum_1", "resource_id": "res_1", "title": "어텐션 정리",
    "summary": "어텐션은 토큰 관련도를 계산한다.", "key_insights": ["멀티헤드는 표현 다양성을 높인다"],
    "domain": "AI", "status": "완료", "created_at": "2026-06-08T09:30:00+09:00",
}
VALID_POST = {
    "post_id": "post_1", "title": "어텐션 가이드", "slug": "attention-guide",
    "body": "## 개요\n어텐션 설명", "summary_id": "sum_1", "status": "발행",
    "tags": ["AI"], "author": "백요한", "published_at": "2026-06-08T09:30:00+09:00",
}


def test_quality_score_full_for_valid_summary():
    v = V.verify_entity("summary", VALID_SUMMARY, VALIDATOR, LINKS)
    assert v["schema_valid"] is True
    assert v["quality_score"] == 6
    assert v["rulebook_pass"] is True
    assert v["cross_links_intact"] is True
    assert v["contradiction_detected"] is False
    assert set(v["quality_checklist"]) == {
        "schema_valid", "required_present", "enums_valid",
        "provenance_present", "timestamps_valid", "content_nonempty",
    }


def test_quality_score_drops_on_bad_enum():
    bad = dict(VALID_SUMMARY, domain="우주")  # Domain enum 위반
    cl = V.quality_checklist("summary", bad, VALIDATOR)
    assert cl["enums_valid"] is False
    assert cl["schema_valid"] is False
    assert sum(cl.values()) < 6


def test_rulebook_post_requires_summary_id():
    no_src = dict(VALID_POST)
    no_src.pop("summary_id")
    ok, violations = V.rulebook_check("post", no_src, VALIDATOR)
    assert ok is False
    assert any("summary_id" in m for m in violations)
    ok2, _ = V.rulebook_check("post", VALID_POST, VALIDATOR)
    assert ok2 is True


def test_contradiction_summary_done_without_insights():
    bad = dict(VALID_SUMMARY, key_insights=[], status="완료")
    assert V.detect_contradiction("summary", bad, VALIDATOR) is True
    assert V.detect_contradiction("summary", VALID_SUMMARY, VALIDATOR) is False


def test_contradiction_confidence_out_of_range():
    bad = dict(VALID_SUMMARY, confidence=1.5)
    assert V.detect_contradiction("summary", bad, VALIDATOR) is True


def test_cross_links_intact_true_for_known_fk():
    # summary.resource_id → summarized_by 링크 선언됨
    assert V.cross_links_intact("summary", VALID_SUMMARY, VALIDATOR, LINKS) is True
    # post.summary_id → published_as 링크 선언됨
    assert V.cross_links_intact("post", VALID_POST, VALIDATOR, LINKS) is True


def test_cross_links_intact_false_when_link_missing():
    # 링크 맵이 비면 FK(resource_id) 가 떠 있어 무결성 깨짐
    assert V.cross_links_intact("summary", VALID_SUMMARY, VALIDATOR, []) is False
    # FK 없는 데이터(자기 PK만)면 빈 링크맵에서도 무결
    pk_only = {"summary_id": "s", "title": "t"}
    assert V.cross_links_intact("summary", pk_only, VALIDATOR, []) is True


def test_make_envelope_entity_full_block():
    env = V.make_envelope(
        VALID_POST, sources_used=["studio"], type_="post",
        validator=VALIDATOR, schema_links=LINKS, reasoning_steps=["변환", "발행"],
    )
    assert set(env) >= {"data", "verification", "provenance"}
    ver = env["verification"]
    assert set(ver) >= {"schema_valid", "rulebook_pass", "cross_links_intact",
                        "contradiction_detected", "quality_score"}
    assert isinstance(ver["quality_score"], int)
    assert env["provenance"]["reasoning_steps"] == ["변환", "발행"]


def test_make_envelope_aggregate_neutral():
    env = V.make_envelope(
        {"results": []}, sources_used=["notion", "memory"], schema_valid=True
    )
    ver = env["verification"]
    assert ver["schema_valid"] is True
    assert ver["quality_score"] is None  # 집계 도구 = 비적용
    assert ver["rulebook_pass"] is True and ver["contradiction_detected"] is False


def test_make_envelope_diff_and_errors():
    env = V.make_envelope(
        None, sources_used=[], schema_valid=False, errors=["bad"],
        diff={"before": None, "after": {"x": 1}},
    )
    assert env["errors"] == ["bad"]
    assert env["diff"] == {"before": None, "after": {"x": 1}}

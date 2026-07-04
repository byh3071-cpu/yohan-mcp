# -*- coding: utf-8 -*-
"""core/triple_map.py — triple-map.md 파서(팔레트+트리플 표 구분) + 문장화."""
from core.triple_map import parse_triple_map, sentence_for_triple

_SAMPLE = """\
---
id: triple-map
---

# 트리플 맵

## 표준 Relation 팔레트

| 코드 | 의미 | | 코드 | 의미 |
|------|------|-|------|------|
| `is_a` | ~의 하위 개념 | | `solves` | ~를 해결한다 |
| `embodies` | ~의 기관 역할을 한다 (인체모형 전용) | | `opposite_of` | ~와 대립된다 |
| `comprises` | ~로 구성된다 | | | |

## 트리플 (최신이 아래)

| Subject | Relation | Object | 도메인 | 신뢰도 | 출처 (insight id 또는 경로) | 등록일 |
|---------|----------|--------|--------|--------|------------------------------|--------|
| yohan-brain | embodies | 뇌 (기억·정본이 사는 곳) | AI/자동화 | 5 | memory/soul.yaml#body_map | 2026-07-04 |
| yohan-voice | embodies | 입 (콘텐츠 발화 기관) | AI/자동화 | 4 | 스프린트 D7 | 2026-07-04 |
| 요한 OS | is_a | 팔란티어의 축소판 | AI/자동화 | 3 | palantir-ontology | 2026-06-12 |
| 미확인개념 | unknown_rel | 알수없는대상 | 학습 | 2 | test | 2026-07-04 |
"""


def test_parse_palette():
    palette, _ = parse_triple_map(_SAMPLE)
    assert palette["is_a"] == "~의 하위 개념"
    assert palette["embodies"] == "~의 기관 역할을 한다 (인체모형 전용)"
    assert palette["opposite_of"] == "~와 대립된다"
    assert palette["comprises"] == "~로 구성된다"
    assert "solves" in palette


def test_parse_triples_fields():
    _, triples = parse_triple_map(_SAMPLE)
    assert len(triples) == 4
    brain = next(t for t in triples if t["subject"] == "yohan-brain")
    assert brain["relation"] == "embodies"
    assert brain["object"] == "뇌 (기억·정본이 사는 곳)"
    assert brain["domain"] == "AI/자동화"
    assert brain["confidence"] == 5
    assert brain["source"] == "memory/soul.yaml#body_map"


def test_parse_triples_does_not_pick_up_palette_rows():
    _, triples = parse_triple_map(_SAMPLE)
    subjects = {t["subject"] for t in triples}
    assert "is_a" not in subjects and "embodies" not in subjects


def test_sentence_for_triple_embodies():
    palette, _ = parse_triple_map(_SAMPLE)
    sent = sentence_for_triple("yohan-voice", "embodies", "입 (콘텐츠 발화 기관)", palette)
    assert sent == "yohan-voice는 입 (콘텐츠 발화 기관)의 기관 역할을 한다 (인체모형 전용)"


def test_sentence_for_triple_is_a():
    palette, _ = parse_triple_map(_SAMPLE)
    sent = sentence_for_triple("요한 OS", "is_a", "팔란티어의 축소판", palette)
    assert sent == "요한 OS는 팔란티어의 축소판의 하위 개념"


def test_sentence_for_unknown_relation_falls_back_gracefully():
    palette, _ = parse_triple_map(_SAMPLE)
    sent = sentence_for_triple("미확인개념", "unknown_rel", "알수없는대상", palette)
    assert "미확인개념" in sent and "알수없는대상" in sent


def test_confidence_unparseable_becomes_none():
    text = _SAMPLE.replace("| AI/자동화 | 5 |", "| AI/자동화 | 오류 |", 1)
    _, triples = parse_triple_map(text)
    bad = next(t for t in triples if t["subject"] == "yohan-brain")
    assert bad["confidence"] is None


def test_empty_text():
    palette, triples = parse_triple_map("")
    assert palette == {} and triples == []

# -*- coding: utf-8 -*-
"""yohan-mcp v2 — brain `memory/knowledge-hub/triple-map.md` 파서 (ontology_triples 시드용).

triple-map.md 에는 markdown 표가 2개 있다:
  1. "표준 Relation 팔레트" — relation 코드 → 의미(한글) 범례. 5열(코드|의미|(spacer)|코드|의미)로
     2쌍을 한 행에 나란히 적어 지면을 아낀다.
  2. "트리플" 본표 — Subject|Relation|Object|도메인|신뢰도|출처|등록일.

섹션 제목 텍스트가 아니라 **표 헤더 셀 자체**로 두 표를 구분한다(제목 문구가 바뀌어도 안전).
팔레트는 하드코딩하지 않고 파일에서 직접 파싱 — 파일이 SoT 라 드리프트가 없다. 트리플 표에
없는 relation 코드를 만나면 팔레트에 없어도 안전한 기본 문구로 폴백한다(하드페일 아님 — 문서
파싱은 최선노력, 실패해도 sentence 는 만들어진다).
"""
from __future__ import annotations

import re

_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _split_row(line: str) -> list[str]:
    """markdown 표 한 행 → 셀 리스트(양끝 파이프 제거 후 strip)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    non_empty = [c for c in cells if c]
    return bool(non_empty) and all(_SEP_CELL_RE.match(c) for c in non_empty)


def _extract_tables(text: str) -> list[list[list[str]]]:
    """텍스트에서 연속된 `|` 행 묶음(표) 들을 찾아 각각 [행][셀] 형태로 반환."""
    tables: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for line in text.split("\n"):
        if line.strip().startswith("|"):
            cur.append(_split_row(line))
        else:
            if cur:
                tables.append(cur)
                cur = []
    if cur:
        tables.append(cur)
    return tables


def _parse_palette_table(rows: list[list[str]]) -> dict[str, str]:
    """범례 표(5열, 2쌍/행) → {relation_code: meaning}. 스페이서 빈 셀은 필터링 후 2개씩 페어링."""
    palette: dict[str, str] = {}
    for row in rows:
        if _is_separator_row(row):
            continue
        cells = [c for c in row if c]  # 스페이서(빈 문자열 컬럼) 제거
        # 헤더 행("코드","의미"...) 은 건너뜀
        if cells[:2] == ["코드", "의미"]:
            continue
        for k in range(0, len(cells) - 1, 2):
            code = cells[k].strip("`").strip()
            meaning = cells[k + 1].strip()
            if code:
                palette[code] = meaning
    return palette


def _parse_triples_table(rows: list[list[str]]) -> list[dict]:
    """본표(Subject|Relation|Object|도메인|신뢰도|출처|등록일) → 트리플 dict 리스트."""
    triples: list[dict] = []
    for row in rows:
        if _is_separator_row(row):
            continue
        if len(row) < 3:
            continue
        subject, relation, obj = row[0].strip(), row[1].strip().strip("`"), row[2].strip()
        if subject in ("Subject", "") or relation == "" or obj == "":
            continue  # 헤더 행 또는 결측 필수 필드 — 스킵(graceful)
        domain = row[3].strip() if len(row) > 3 else ""
        conf_raw = row[4].strip() if len(row) > 4 else ""
        source = row[5].strip() if len(row) > 5 else ""
        registered_at = row[6].strip() if len(row) > 6 else ""
        try:
            confidence = int(conf_raw)
        except ValueError:
            confidence = None
        triples.append({
            "subject": subject,
            "relation": relation,
            "object": obj,
            "domain": domain,
            "confidence": confidence,
            "source": source,
            "registered_at": registered_at,
        })
    return triples


def parse_triple_map(text: str) -> tuple[dict[str, str], list[dict]]:
    """triple-map.md 전문 → (relation 팔레트 dict, 트리플 리스트).

    표는 헤더 셀 내용으로 식별한다(제목 텍스트 무관):
    - 팔레트: 첫 두 비어있지 않은 헤더 셀이 "코드","의미".
    - 트리플: 첫 세 헤더 셀이 정확히 "Subject","Relation","Object"(영문, 파일의 실제 표기).
    다른 표(우연히 `|` 로 시작하는 무관 블록)는 조용히 무시한다.
    """
    palette: dict[str, str] = {}
    triples: list[dict] = []
    for rows in _extract_tables(text):
        if not rows:
            continue
        header = rows[0]
        header_cells = [c for c in header if c]
        if header_cells[:3] == ["Subject", "Relation", "Object"]:
            triples = _parse_triples_table(rows[1:])
        elif header_cells[:2] == ["코드", "의미"]:
            palette = _parse_palette_table(rows[1:])
    return palette, triples


def sentence_for_triple(subject: str, relation: str, obj: str, palette: dict[str, str]) -> str:
    """트리플 → 문장화 텍스트 "{Subject}는 {Object}{Relation 의미}" (임베딩 입력용).

    팔레트의 의미 문구는 관례상 "~"(Object 자리)로 시작한다(예: "~의 하위 개념"). 그 "~" 를
    떼어 Object 뒤에 바로 이어 붙이면 자연스러운 한 문장이 된다. 팔레트에 없는 relation 코드는
    안전한 기본 문구로 폴백(하드페일 아님 — 문서 드리프트에 관대).
    """
    meaning = palette.get(relation)
    if meaning is None:
        return f"{subject}는 {obj}와(과) {relation} 관계다"
    suffix = meaning[1:] if meaning.startswith("~") else f" {meaning}"
    return f"{subject}는 {obj}{suffix}"

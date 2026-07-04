# -*- coding: utf-8 -*-
"""yohan-mcp v2 — markdown-aware 청킹 (brain memory/ 벡터 인제스트용, 스프린트 필수 코어 ②).

목표: 512토큰/50토큰오버랩. 토큰 카운터는 실토크나이저가 아니라 근사치다
(CJK 문자 ≈ 1토큰/글자, 그 외(영문 등) ≈ 1토큰/4글자) — bge-m3 실토크나이저를 호출하지 않고도
청크 크기를 가늠하기 위한 결정적 근사.

원자 단위(절대 안 쪼갬): 코드펜스(```/~~~ 블록) · 표(연속된 `|` 행) · 선두 frontmatter(--- 블록).
원자 블록이 512토큰을 넘어도 통짜로 유지한다 — bge-m3 컨텍스트 한도(8192) 내에서 안전하다는
전제(스프린트 결정사항). 그 외 본문은 빈 줄 기준 문단으로 나눈 뒤 512토큰 한도까지 그리디하게
병합하고, 청크 경계마다 이전 청크 꼬리의 ~50토큰을 다음 청크 머리에 이월(overlap)한다.

`chunk_markdown(text, base_line=N)` 의 `start_line` 은 **`text` 인자 기준 1-indexed** 줄번호에
`base_line-1` 을 더한 값 — 호출자가 frontmatter 를 미리 떼어낸 본문(body)을 넘길 때도 원본
파일 기준 역링크(chunk_start_line)를 정확히 재구성할 수 있게 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_TOKENS = 512
OVERLAP_TOKENS = 50

# CJK(한글 포함) 코드포인트 범위 — 대략치 토큰추정용(정밀 유니코드 스크립트 분류 아님).
_CJK_RANGES = (
    (0x1100, 0x11FF),   # Hangul Jamo
    (0x3040, 0x30FF),   # Hiragana/Katakana
    (0x3130, 0x318F),   # Hangul Compatibility Jamo
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xAC00, 0xD7A3),   # Hangul Syllables
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),   # Halfwidth/Fullwidth Forms
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """대략적 토큰 수 추정 — CJK 1글자=1토큰, 그 외 4글자≈1토큰(올림).

    실 토크나이저(bge-m3) 호출 없이 청킹 크기를 가늠하는 근사치. 청킹 결정(원자 블록 보존 등)
    용도로만 쓰이며, 임베딩 자체는 core.embeddings 가 처리한다.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + -(-other // 4)  # ceil(other / 4), 표준라이브러리 math 의존 없이


@dataclass
class _Segment:
    kind: str        # "frontmatter" | "code" | "table" | "para"
    text: str
    start_line: int  # 1-indexed, 인자로 받은 text 기준


@dataclass
class Chunk:
    text: str
    start_line: int       # 1-indexed — chunk_markdown 호출자가 지정한 base_line 반영됨
    token_estimate: int


_FENCE_RE = re.compile(r"^(```+|~~~+)")


def _segment_markdown(text: str) -> list[_Segment]:
    """텍스트를 원자 단위(frontmatter/code/table) + 문단(para) 세그먼트로 분해.

    각 세그먼트의 start_line 은 이 함수에 넘겨진 `text` 기준 1-indexed 줄번호다
    (원본 파일 기준 오프셋은 chunk_markdown 의 base_line 이 처리).
    """
    if not text:
        return []
    lines = text.split("\n")
    n = len(lines)
    segments: list[_Segment] = []
    i = 0

    # 1) 선두 frontmatter 블록 — 순수 구조 판정(YAML 유효성 무관), 파일 맨 앞일 때만.
    if n > 0 and lines[0].strip() == "---":
        for j in range(1, n):
            if lines[j].strip() == "---":
                segments.append(_Segment("frontmatter", "\n".join(lines[0:j + 1]), 1))
                i = j + 1
                break
        # 닫는 --- 못 찾으면 frontmatter 취급 안 함 (i=0 유지, 아래 일반 루프가 처리)

    para_buf: list[str] = []
    para_start: int | None = None

    def _flush_para() -> None:
        nonlocal para_buf, para_start
        if para_buf:
            joined = "\n".join(para_buf)
            if joined.strip():
                segments.append(_Segment("para", joined, para_start or 1))
        para_buf = []
        para_start = None

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 코드펜스 — 여는 fence 부터 같은 종류의 닫는 fence(또는 EOF)까지 원자.
        m = _FENCE_RE.match(stripped)
        if m:
            _flush_para()
            fence = m.group(1)[0] * 3  # ``` 또는 ~~~ 로 정규화(4개 이상 백틱도 동일 취급)
            start = i
            j = i + 1
            while j < n and not lines[j].strip().startswith(fence):
                j += 1
            end = j if j < n else n - 1  # 닫는 fence 없으면 EOF 까지 관대하게 흡수
            segments.append(_Segment("code", "\n".join(lines[start:end + 1]), start + 1))
            i = end + 1
            continue

        # 표 — 연속된 '|' 시작 줄 묶음(헤더/구분선/데이터행 구분 없이 통짜 원자).
        if stripped.startswith("|"):
            _flush_para()
            start = i
            j = i
            while j < n and lines[j].strip().startswith("|"):
                j += 1
            segments.append(_Segment("table", "\n".join(lines[start:j]), start + 1))
            i = j
            continue

        # 빈 줄 — 문단 경계.
        if not stripped:
            _flush_para()
            i += 1
            continue

        # 일반 본문 줄 — 문단 버퍼에 축적.
        if para_start is None:
            para_start = i + 1
        para_buf.append(line)
        i += 1

    _flush_para()
    return segments


def _tail_overlap(cur: list[_Segment], overlap: int, target: int) -> list[_Segment]:
    """직전 청크 세그먼트 목록의 꼬리에서 ~overlap 토큰만큼 이월할 세그먼트를 뽑는다.

    원자 세그먼트는 쪼개지 않으므로 overlap 은 '세그먼트 단위'로 근사한다 — 마지막 세그먼트 하나가
    이미 overlap 을 넘으면 그것 하나만 이월(그 이상 끌어오지 않음). 단, 그 마지막 세그먼트가
    target 급으로 거대한 원자 블록(큰 표/코드펜스)이면 이월 자체를 생략한다 — 그러지 않으면
    거대 블록이 직전 청크와 다음 청크에 통째로 두 번 중복 저장된다(임베딩/스토리지 낭비).
    """
    tail: list[_Segment] = []
    total = 0
    for seg in reversed(cur):
        tok = estimate_tokens(seg.text)
        if not tail and tok > target:
            break  # 거대 원자 블록은 이월하지 않음(중복 방지) — 다음 청크는 빈 상태로 시작
        if tail and total >= overlap:
            break
        tail.append(seg)
        total += tok
    tail.reverse()
    return tail


def pack_chunks(
    segments: list[_Segment], target: int = TARGET_TOKENS, overlap: int = OVERLAP_TOKENS
) -> list[Chunk]:
    """세그먼트를 target 토큰 한도로 그리디 병합 + 청크 경계 overlap 이월.

    원자 세그먼트(frontmatter/code/table) 는 절대 쪼개지 않는다 — 하나가 target 을 넘으면
    그 세그먼트만으로 이뤄진 청크가 된다(bge-m3 8192 한도 내 안전, 스프린트 결정).
    """
    chunks: list[Chunk] = []
    cur: list[_Segment] = []
    cur_tokens = 0

    def _emit() -> None:
        if not cur:
            return
        text = "\n\n".join(s.text for s in cur)
        if text.strip():
            chunks.append(Chunk(text=text, start_line=cur[0].start_line, token_estimate=cur_tokens))

    for seg in segments:
        seg_tok = estimate_tokens(seg.text)
        if cur and cur_tokens + seg_tok > target:
            _emit()
            tail = _tail_overlap(cur, overlap, target)
            cur = tail
            cur_tokens = sum(estimate_tokens(s.text) for s in cur)
        cur.append(seg)
        cur_tokens += seg_tok

    _emit()
    return chunks


def chunk_markdown(
    text: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    base_line: int = 1,
) -> list[Chunk]:
    """markdown 텍스트 → Chunk 리스트. 공개 API.

    base_line: 이 `text` 의 1번째 줄이 실제로는 원본 파일의 몇 번째 줄인지(1-indexed).
    호출자가 frontmatter 를 이미 떼어낸 본문(body)만 넘길 때, 역링크(chunk_start_line)가
    원본 파일 기준으로 정확하도록 보정한다. 기본값 1 = 넘긴 text 자체가 원본(오프셋 없음).
    """
    segments = _segment_markdown(text or "")
    if not segments:
        return []
    chunks = pack_chunks(segments, target=target_tokens, overlap=overlap_tokens)
    if base_line != 1:
        offset = base_line - 1
        chunks = [
            Chunk(text=c.text, start_line=c.start_line + offset, token_estimate=c.token_estimate)
            for c in chunks
        ]
    return chunks

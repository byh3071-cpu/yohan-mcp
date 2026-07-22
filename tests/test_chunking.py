# -*- coding: utf-8 -*-
"""core/chunking.py — md-aware 청킹: 원자 블록(코드펜스·표·frontmatter) 보존 + 토큰추정 + overlap."""
from core.chunking import (
    MAX_SEGMENT_TOKENS,
    _Segment,
    _segment_markdown,
    _split_oversized,
    chunk_markdown,
    estimate_tokens,
)


def _heavy_text(n_paragraphs: int = 20) -> str:
    return "\n\n".join(f"문단내용{i} " * 20 for i in range(n_paragraphs))


# ── 토큰 추정 ────────────────────────────────────────────────────
def test_estimate_tokens_cjk_counts_one_per_char():
    assert estimate_tokens("가나다") == 3


def test_estimate_tokens_ascii_counts_quarter_per_char():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


# ── 세그먼트 분해(줄번호) ────────────────────────────────────────
def test_segment_markdown_line_numbers():
    text = "line1\nline2\n\nline4\nline5"
    segs = _segment_markdown(text)
    assert segs[0].start_line == 1
    assert segs[1].start_line == 4


# ── 원자 블록 보존 ───────────────────────────────────────────────
def test_code_fence_kept_atomic():
    text = "문단1\n\n```python\ndef f():\n    return 1\n\n# blank line inside fence\n```\n\n문단2"
    chunks = chunk_markdown(text)
    fence_chunks = [c for c in chunks if "def f():" in c.text]
    assert len(fence_chunks) == 1
    assert fence_chunks[0].text.count("```") == 2


def test_tilde_fence_supported():
    text = "문단.\n\n~~~js\nconsole.log(1)\n~~~\n\n문단2."
    chunks = chunk_markdown(text)
    fence_chunks = [c for c in chunks if "console.log" in c.text]
    assert len(fence_chunks) == 1
    assert fence_chunks[0].text.count("~~~") == 2


def test_unclosed_fence_absorbed_to_eof():
    text = "문단.\n\n```python\ndef f():\n    pass\n"
    chunks = chunk_markdown(text)
    assert any("def f()" in c.text for c in chunks)


def test_pipe_inside_code_fence_not_treated_as_table():
    text = "```md\n| a | b |\n|---|---|\n```"
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].text.count("```") == 2


def test_table_kept_atomic():
    text = "문단1\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n문단2"
    chunks = chunk_markdown(text)
    table_chunks = [c for c in chunks if "| a | b |" in c.text]
    assert len(table_chunks) == 1
    assert all(row in table_chunks[0].text for row in ["| 1 | 2 |", "| 3 | 4 |"])


def test_frontmatter_kept_atomic():
    text = "---\nid: x\ntitle: hello\n---\n\n본문 시작" + ("\n\n문단." * 5)
    chunks = chunk_markdown(text)
    fm_chunks = [c for c in chunks if c.text.startswith("---")]
    assert len(fm_chunks) == 1
    assert "title: hello" in fm_chunks[0].text


def test_three_code_fences_preserved():
    """스프린트 명세 — 코드펜스 3개 낀 md 에서 각 펜스가 쪼개지지 않고 원문 그대로 보존되는지.

    문단을 충분히 무겁게 만들어 여러 청크로 나뉘게 강제한 뒤, 그래도 각 펜스(여는~닫는 마커
    포함)가 어느 한 청크 안에 통짜로(쪼개지지 않고) 들어있는지 검증한다.
    """
    fences = [f"```lang{i}\nline a\n\nline b (blank above)\n```" for i in range(3)]
    parts = []
    for i in range(3):
        parts.append(f"설명 문단 {i} 반복 내용 채우기." * 60)  # 청크 여러 개로 쪼개질 만큼 무겁게
        parts.append(fences[i])
    text = "\n\n".join(parts)
    chunks = chunk_markdown(text)
    assert len(chunks) >= 3, "테스트 전제(여러 청크로 분할) 불충족 — 문단을 더 키워야 함"
    for i, fence in enumerate(fences):
        assert any(fence in c.text for c in chunks), f"fence {i} 가 쪼개짐(원문 그대로 보존 안 됨)"


def test_oversized_atomic_block_kept_whole():
    """target(512) 은 넘되 MAX_SEGMENT_TOKENS(1024) 이하인 원자 블록은 통짜로 유지.

    이 테스트는 원래 400행(1,845토큰)이었다. 그 크기가 실제로 ollama 임베딩을 죽인다는 게
    확인돼(MAX_SEGMENT_TOKENS 주석) 한도 이하인 200행(895토큰)으로 낮췄다 — '원자 블록은
    무조건 통짜' 가 아니라 '한도까지는 통짜' 로 계약이 바뀌었다. 한도 초과분은 아래 테스트가 맡는다.
    """
    table_rows = "\n".join(f"| r{i} | 값{i} |" for i in range(200))
    text = "짧은 문단.\n\n" + table_rows + "\n\n짧은 문단2."
    chunks = chunk_markdown(text)
    big = [c for c in chunks if "r0 |" in c.text and "r199 |" in c.text]
    assert len(big) == 1
    assert 512 < big[0].token_estimate <= MAX_SEGMENT_TOKENS


# ── 세그먼트 상한(MAX_SEGMENT_TOKENS) ────────────────────────────
# 회귀 배경: 상한이 없어 24,998토큰짜리 문단이 그대로 임베딩으로 넘어갔고, ollama 가 출력 버퍼를
# 못 잡아 NaN → 500 을 뱉으며 brain 시딩이 167/355 에서 4회 연속 죽었다.


def _no_blank_line_paragraph(n_lines: int) -> str:
    """빈 줄이 하나도 없는 긴 본문 — 웹 스크랩 문서의 실제 형태(문단 경계가 안 잡힌다)."""
    return "\n".join(f"본문 {i} 번째 줄이며 문단 경계가 없다." for i in range(n_lines))


def test_paragraph_without_blank_lines_is_capped():
    """실패 재현 — 빈 줄 없는 대형 본문이 통짜 세그먼트가 돼도 청크는 한도를 안 넘어야."""
    text = _no_blank_line_paragraph(2000)
    assert estimate_tokens(text) > 10 * MAX_SEGMENT_TOKENS, "테스트 전제 불충족"
    chunks = chunk_markdown(text)
    assert len(chunks) > 1
    for c in chunks:
        assert estimate_tokens(c.text) <= MAX_SEGMENT_TOKENS


def test_oversized_code_fence_is_split():
    """원자 블록이라도 한도를 넘으면 쪼갠다 — 코드펜스도 예외 아님."""
    body = "\n".join(f"    line_{i} = compute(value_{i})" for i in range(1500))
    text = f"설명 문단.\n\n```python\n{body}\n```"
    chunks = chunk_markdown(text)
    assert len(chunks) > 1
    for c in chunks:
        assert estimate_tokens(c.text) <= MAX_SEGMENT_TOKENS


def test_previously_fatal_table_size_now_splits():
    """1,845토큰 표 — 실측상 임베딩이 죽는 크기(최초 실패 파일 1,808토큰). 이제 쪼개져야 한다."""
    rows = "\n".join(f"| r{i} | 값{i} |" for i in range(400))
    assert 1808 <= estimate_tokens(rows), "테스트 전제(실패 임계 초과) 불충족"
    chunks = chunk_markdown(rows)
    assert len(chunks) > 1
    for c in chunks:
        assert estimate_tokens(c.text) <= MAX_SEGMENT_TOKENS


def test_chunk_cap_holds_when_big_segment_follows_normal_text():
    """세그먼트 상한만으로는 부족하다 — 앞 문단(≤512) + 거대 세그먼트(≤1024)가 한 청크로 합쳐지면
    1,024를 넘는다. 실측 스윕에서 이 경로로 1,445토큰 청크가 나왔다. 거대 세그먼트는 단독 청크여야.
    """
    # 이월되는 꼬리 문단이 커야 재현된다(_tail_overlap 은 마지막 세그먼트를 통째로 이월).
    lead = "\n\n".join("앞 문단 내용을 채운다." * 40 for _ in range(3))  # 문단당 ~400토큰
    rows = "\n".join(f"| r{i} | 값{i} |" for i in range(200))  # 895토큰 원자 블록
    chunks = chunk_markdown(f"{lead}\n\n{rows}")
    assert len(chunks) > 1
    for c in chunks:
        assert estimate_tokens(c.text) <= MAX_SEGMENT_TOKENS


def test_split_oversized_is_noop_under_limit():
    seg = _Segment("para", "짧은 본문.", 7)
    assert _split_oversized(seg, MAX_SEGMENT_TOKENS) == [seg]


def test_split_oversized_preserves_text_exactly():
    """조각을 개행으로 다시 이으면 원문과 정확히 일치 — 분할 과정에서 유실/중복 없음."""
    original = _no_blank_line_paragraph(1200)
    parts = _split_oversized(_Segment("para", original, 1), MAX_SEGMENT_TOKENS)
    assert len(parts) > 1
    assert "\n".join(p.text for p in parts) == original


def test_split_oversized_start_lines_point_at_real_lines():
    """조각의 start_line 이 원문의 실제 줄을 가리켜야 역링크(chunk_start_line)가 안 깨진다."""
    base = 36  # 실제 실패 파일(url-0251d36410027d64.md)의 문단 시작 줄
    lines = _no_blank_line_paragraph(1200).split("\n")
    parts = _split_oversized(_Segment("para", "\n".join(lines), base), MAX_SEGMENT_TOKENS)
    assert len(parts) > 1
    assert parts[0].start_line == base
    for p in parts:
        assert p.text.split("\n")[0] == lines[p.start_line - base]


def test_split_oversized_keeps_kind():
    parts = _split_oversized(_Segment("table", _no_blank_line_paragraph(1200), 1), MAX_SEGMENT_TOKENS)
    assert len(parts) > 1
    assert all(p.kind == "table" for p in parts)


def test_single_line_longer_than_limit_is_split():
    """개행 없는 거대 blob(민화된 JSON 등) — 줄 단위로 못 자르면 글자 단위 절단으로 방어."""
    blob = "데이터" * 5000  # 개행 0개, 약 15,000토큰
    parts = _split_oversized(_Segment("para", blob, 1), MAX_SEGMENT_TOKENS)
    assert len(parts) > 1
    for p in parts:
        assert estimate_tokens(p.text) <= MAX_SEGMENT_TOKENS
    assert "".join(p.text for p in parts) == blob  # 글자 유실 없음


# ── 병합(target) + overlap ───────────────────────────────────────
def test_pack_merges_short_paragraphs_and_stays_near_target():
    chunks = chunk_markdown(_heavy_text(20))
    assert len(chunks) >= 2
    for c in chunks[:-1]:
        assert c.token_estimate <= 512 + 100  # target + overlap 이월분 여유


def test_overlap_between_consecutive_chunks():
    chunks = chunk_markdown(_heavy_text(20))
    assert len(chunks) >= 2
    tail_snippet = chunks[0].text.strip().split("\n\n")[-1]
    assert tail_snippet in chunks[1].text


# ── start_line / base_line ───────────────────────────────────────
def test_start_line_reported_within_given_text():
    text = "첫줄.\n\n둘째 문단."
    chunks = chunk_markdown(text)
    assert chunks[0].start_line == 1


def test_base_line_offset_applied():
    text = "본문 첫줄.\n\n둘째 문단."
    default_chunks = chunk_markdown(text)
    offset_chunks = chunk_markdown(text, base_line=12)
    assert offset_chunks[0].start_line == default_chunks[0].start_line + 11


def test_empty_text_returns_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown(None) == []

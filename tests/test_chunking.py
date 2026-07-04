# -*- coding: utf-8 -*-
"""core/chunking.py — md-aware 청킹: 원자 블록(코드펜스·표·frontmatter) 보존 + 토큰추정 + overlap."""
from core.chunking import _segment_markdown, chunk_markdown, estimate_tokens


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
    huge_table_rows = "\n".join(f"| r{i} | 값{i} |" for i in range(400))
    text = "짧은 문단.\n\n" + huge_table_rows + "\n\n짧은 문단2."
    chunks = chunk_markdown(text)
    big = [c for c in chunks if "r0 |" in c.text and "r399 |" in c.text]
    assert len(big) == 1  # 512토큰 넘어도 안 쪼개짐(표 전체가 한 청크)
    assert big[0].token_estimate > 512


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

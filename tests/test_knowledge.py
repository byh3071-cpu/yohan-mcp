# -*- coding: utf-8 -*-
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

import httpx
import pytest

from core import knowledge as knowledge_module
from core.knowledge import (
    BrainWriter,
    CaptionCue,
    CaptionEvidence,
    CaptionEvidenceError,
    FocusFeedQueue,
    KnowledgeError,
    KnowledgeService,
    NotebookLmClient,
    NotebookRegistry,
    RegistrySnapshot,
    ReviewStore,
    SourceInfo,
    YoutubeCaptionProvider,
    canonical_url,
    extract_youtube_source_identities,
    evaluate_draft,
    ground_draft_with_caption_evidence,
    ground_draft_with_source_evidence,
    processing_failure_code,
    run_notebooklm_command,
    run_notebooklm_source_identity_helper,
    run_ytdlp_caption_command,
    youtube_video_id,
)
from core.paths import resolve_knowledge_runtime_dir
from scripts import knowledge as knowledge_cli
from scripts.knowledge import configure_utf8_stdio
from scripts.knowledge import build_parser


JOB_ID = "11111111-1111-4111-8111-111111111111"
OWNER_USER_ID = "22222222-2222-4222-8222-222222222222"
VIDEO_ID = "abcDEF_1234"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_cli_configures_utf8_for_windows_pipe_output() -> None:
    calls: list[dict[str, str]] = []

    class LegacyStream:
        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    configure_utf8_stdio(LegacyStream(), LegacyStream())

    assert calls == [
        {"encoding": "utf-8", "errors": "backslashreplace"},
        {"encoding": "utf-8", "errors": "backslashreplace"},
    ]


def valid_draft() -> dict[str, Any]:
    return {
        "title": "테스트 영상",
        "summary": "원문에 충실한 충분히 긴 요약입니다. " * 12,
        "key_points": ["핵심 하나", "핵심 둘", "핵심 셋"],
        "claims": [
            {
                "type": "fact",
                "statement": "검증 가능한 사실",
                "evidence_quote": "시작 구간에서 검증 가능한 사실을 자세히 설명합니다 지금",
                "citation": "[99:99]",
            }
        ],
        "coverage": {
            "start": {
                "statement": "시작 설명",
                "evidence_quote": "시작 구간에서 검증 가능한 사실을 자세히 설명합니다 지금",
            },
            "middle": {
                "statement": "중간 설명",
                "evidence_quote": "중간 구간에서는 실행 방법과 중요한 제약을 설명합니다 지금",
            },
            "end": {
                "statement": "끝 설명",
                "evidence_quote": "마지막 구간에서는 결론과 다음 행동을 명확하게 정리합니다 지금",
            },
        },
        "yohan_relevance": "요한의 1인 AI 운영에서 반복 업무를 줄이는 데 적용할 수 있습니다.",
        "uncertainties": ["없음"],
        "promotion_candidates": {"concepts": [], "people": [], "triples": []},
    }


def transcript() -> str:
    draft = valid_draft()
    return " ".join(
        (
            draft["coverage"]["start"]["evidence_quote"],
            "초반 원문 맥락과 검증 설명입니다. " * 80,
            draft["coverage"]["middle"]["evidence_quote"],
            "후반 원문 맥락과 적용 설명입니다. " * 80,
            draft["coverage"]["end"]["evidence_quote"],
        )
    )


def caption_vtt() -> str:
    return """WEBVTT

00:00:10.000 --> 00:00:18.000
시작 구간에서 검증 가능한 사실을 자세히 설명합니다 지금

00:02:30.000 --> 00:02:40.000
중간 구간에서는 실행 방법과 중요한 제약을 설명합니다 지금

00:04:50.000 --> 00:05:00.000
마지막 구간에서는 결론과 다음 행동을 명확하게 정리합니다 지금
"""


def caption_evidence() -> CaptionEvidence:
    return CaptionEvidence.from_vtt(caption_vtt())


def caption_source_text(evidence: CaptionEvidence | None = None) -> str:
    selected = evidence or caption_evidence()
    return " ".join(cue.text for cue in selected.cues)


def grounded_draft() -> dict[str, Any]:
    return ground_draft_with_caption_evidence(
        valid_draft(),
        caption_evidence(),
    )


def approval_quality(*, tier: str = "T2") -> dict[str, Any]:
    quality: dict[str, Any] = {
        "score": 95,
        "passed": True,
        "evidence_contract": "caption-v1",
        "hard_failures": [],
    }
    if tier == "T3":
        quality["second_evaluator"] = {"passed": True, "name": "independent"}
    return quality


def candidate_contract_response(prompt: str, draft: dict[str, Any]) -> dict[str, Any]:
    payload_line = next(line for line in prompt.splitlines() if line.startswith("EVIDENCE_CANDIDATES="))
    candidates = json.loads(payload_line.split("=", 1)[1])
    by_part = {
        part: next(item for item in candidates if item["part"] == part)
        for part in ("start", "middle", "end")
    }
    result = deepcopy(draft)
    claims = []
    for claim in result["claims"]:
        cleaned = {key: value for key, value in claim.items() if key not in {"evidence_quote", "caption_quote", "citation", "citation_verified"}}
        cleaned.pop("requires_crosscheck", None)
        if cleaned.get("type") == "fact":
            cleaned["evidence_id"] = by_part["start"]["id"]
        claims.append(cleaned)
    result["claims"] = claims
    result["coverage"] = {
        part: {"statement": result["coverage"][part]["statement"], "evidence_id": by_part[part]["id"]}
        for part in ("start", "middle", "end")
    }
    return result


def semantic_verdict_response(prompt: str, *, supported: bool = True) -> dict[str, Any]:
    items_line = next(line for line in prompt.splitlines() if line.startswith("ITEMS="))
    items = json.loads(items_line.split("=", 1)[1])
    return {
        "contract_version": "notebooklm-semantic-verdict-v1",
        "items": [{"id": item["item_id"], "supported": supported} for item in items],
    }


def test_semantic_verdict_accepts_one_exact_json_code_fence() -> None:
    expected = (
        {
            "item_id": "F01",
            "candidate_id": "CS01",
            "quote": "eight grounded words remain immutable for this semantic evidence check",
            "statement": "The quote supports the statement.",
        },
    )
    verdict = {
        "contract_version": "notebooklm-semantic-verdict-v1",
        "items": [{"id": "F01", "supported": True}],
    }

    knowledge_module.validate_semantic_verdict(
        f"```json\n{json.dumps(verdict)}\n```",
        expected,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "Result:\n```json\n{}\n```",
        "```json\n{}\n```\nDone.",
        "```javascript\n{}\n```",
        "```json\n{}\n```\n```json\n{}\n```",
    ],
)
def test_semantic_verdict_rejects_prose_wrong_fence_and_multiple_fences(raw: str) -> None:
    expected = (
        {
            "item_id": "F01",
            "candidate_id": "CS01",
            "quote": "eight grounded words remain immutable for this semantic evidence check",
            "statement": "The quote supports the statement.",
        },
    )

    with pytest.raises(knowledge_module.SemanticEvidenceError):
        knowledge_module.validate_semantic_verdict(raw, expected)


class FakeCaptionProvider:
    def __init__(
        self,
        evidence: CaptionEvidence | None = None,
        error: CaptionEvidenceError | None = None,
    ) -> None:
        self.evidence = evidence or caption_evidence()
        self.error = error
        self.calls: list[str] = []

    def fetch(self, url: str) -> CaptionEvidence:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.evidence


class FakeRunner:
    def __init__(self, *, duplicate: bool = False, draft: dict[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.duplicate = duplicate
        self.draft = draft or valid_draft()

    def __call__(self, args: list[str], timeout: int) -> str:
        del timeout
        self.calls.append(args)
        if args[:2] == ["notebook", "list"]:
            notebooks = [{"id": "nb1", "title": "YT · 미분류 · Inbox"}]
            if self.duplicate:
                notebooks.append({"id": "nb2", "title": "ARCHIVE · YT"})
            return json.dumps({"notebooks": notebooks})
        if args[:2] == ["source", "list"]:
            return json.dumps(
                {
                    "sources": [
                        {"id": f"source-{args[2]}", "title": "기존 영상", "url": VIDEO_URL}
                    ]
                }
            )
        if args[:2] == ["source", "add"]:
            return json.dumps({"source_id": "source-added"})
        if args[:2] == ["source", "get"]:
            return transcript()
        if args[:2] == ["notebook", "query"]:
            prompt = args[3]
            response = (
                semantic_verdict_response(prompt)
                if prompt.startswith("Verdict-only semantic check")
                else candidate_contract_response(prompt, self.draft)
                if "EVIDENCE_CANDIDATES=" in prompt
                else self.draft
            )
            return json.dumps({"answer": json.dumps(response, ensure_ascii=False)})
        raise AssertionError(f"unexpected command: {args}")


class ConcurrentAddRunner:
    """Thread-safe external NotebookLM state for check-then-add race tests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.source_id: str | None = None
        self.add_calls = 0

    def __call__(self, args: list[str], timeout: int) -> str:
        del timeout
        if args[:2] == ["notebook", "list"]:
            return json.dumps({"notebooks": [{"id": "nb1", "title": "YT · 미분류 · Inbox"}]})
        if args[:2] == ["source", "list"]:
            with self.lock:
                source_id = self.source_id
            sources = [] if source_id is None else [{"id": source_id, "title": "영상", "url": VIDEO_URL}]
            return json.dumps({"sources": sources})
        if args[:2] == ["source", "add"]:
            with self.lock:
                self.add_calls += 1
                self.source_id = self.source_id or "source-added"
                source_id = self.source_id
            return json.dumps({"source_id": source_id})
        if args[:2] == ["source", "get"]:
            return transcript()
        if args[:2] == ["notebook", "query"]:
            prompt = args[3]
            response = (
                semantic_verdict_response(prompt)
                if prompt.startswith("Verdict-only semantic check")
                else candidate_contract_response(prompt, valid_draft())
                if "EVIDENCE_CANDIDATES=" in prompt
                else valid_draft()
            )
            return json.dumps({"answer": json.dumps(response, ensure_ascii=False)})
        raise AssertionError(f"unexpected command: {args}")


class FakeQueue:
    def __init__(self, job: dict[str, Any] | None = None) -> None:
        self.job = deepcopy(job or make_job())
        self.claimed = False
        self.completions: list[str] = []

    def claim(self, worker_id: str, limit: int) -> list[dict[str, Any]]:
        assert worker_id.startswith("knowledge-worker:")
        assert 1 <= limit <= 3
        if self.claimed:
            return []
        self.claimed = True
        return [deepcopy(self.job)]

    def claim_exact(self, job_id: str, worker_id: str) -> dict[str, Any]:
        assert job_id == self.job["id"]
        assert worker_id.startswith("knowledge-worker:")
        if self.claimed or self.job.get("status") != "queued":
            raise KnowledgeError("job is not eligible for exact claim")
        self.claimed = True
        self.job["status"] = "processing"
        self.job["attempt_count"] = int(self.job.get("attempt_count") or 0) + 1
        self.job["lease_token"] = "22222222-2222-4222-8222-222222222222"
        return deepcopy(self.job)

    def retry(self, job_id: str) -> dict[str, Any]:
        assert job_id == self.job["id"]
        if self.job.get("status") != "action_required" or int(self.job.get("attempt_count") or 0) >= 3:
            raise KnowledgeError("job is not eligible for retry")
        self.job["status"] = "queued"
        self.job["failure_code"] = None
        self.job["failure_message"] = None
        self.job["quality_score"] = None
        self.job["quality_report"] = {}
        self.job["result"] = {}
        return deepcopy(self.job)

    def invalidate_review(self, job_id: str) -> dict[str, Any]:
        assert job_id == self.job["id"]
        metadata = self.job.get("metadata") if isinstance(self.job.get("metadata"), dict) else {}
        attempt_count = int(self.job.get("attempt_count") or 0)
        if self.job.get("status") != "review_required" or attempt_count > 3 or (
            attempt_count == 3 and metadata.get("_legacy_review_recovery_v1") is True
        ):
            raise KnowledgeError("knowledge review is not eligible for invalidation")
        self.job["status"] = "action_required"
        if attempt_count == 3:
            self.job["attempt_count"] = 2
        self.job.setdefault("metadata", {})["_legacy_review_recovery_v1"] = True
        self.job["failure_code"] = "PUBLIC_CAPTION_TIMESTAMPS_REQUIRED"
        self.job["failure_message"] = "legacy review evidence must be reprocessed"
        self.job["approval_token"] = None
        self.job["approval_started_at"] = None
        self.job["approval_intent_hash"] = None
        self.job["lease_token"] = None
        return deepcopy(self.job)

    def checkpoint(self, job: dict[str, Any], **fields: Any) -> dict[str, Any]:
        if job.get("lease_token") != self.job.get("lease_token"):
            raise AssertionError("lease drift")
        self.job.update({key: value for key, value in fields.items() if value is not None})
        return deepcopy(self.job)

    def complete(self, job: dict[str, Any], status: str, **fields: Any) -> dict[str, Any]:
        assert job.get("lease_token") == self.job.get("lease_token")
        self.job["status"] = status
        self.job["lease_token"] = None
        self.job.update(fields)
        self.completions.append(status)
        return deepcopy(self.job)

    def reviews(self) -> list[dict[str, Any]]:
        return [deepcopy(self.job)] if self.job.get("status") in {"review_required", "approving"} else []

    def get(self, job_id: str) -> dict[str, Any]:
        assert job_id == self.job["id"]
        return deepcopy(self.job)

    def canary_jobs(self, run_id: str) -> list[dict[str, Any]]:
        metadata = self.job.get("metadata") if isinstance(self.job.get("metadata"), dict) else {}
        return [deepcopy(self.job)] if metadata.get("_canary_run_id") == run_id else []

    def begin_approval(self, job: dict[str, Any], intent_hash: str) -> dict[str, Any]:
        assert job["status"] in {"review_required", "approving"}
        if self.job["status"] == "review_required":
            self.job["status"] = "approving"
            self.job["approval_token"] = "33333333-3333-4333-8333-333333333333"
            self.job["approval_started_at"] = "2026-08-05T00:00:00Z"
            self.job["approval_intent_hash"] = intent_hash
        elif self.job.get("approval_intent_hash") != intent_hash:
            raise KnowledgeError("approval already in progress with different intent")
        return deepcopy(self.job)

    def mark_completed(self, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        assert job["status"] == "approving"
        assert job["approval_token"] == self.job["approval_token"]
        self.job["status"] = "completed"
        self.job["result"] = dict(result)
        return deepcopy(self.job)

    def defer(self, job: dict[str, Any]) -> dict[str, Any]:
        self.job.setdefault("result", {})["review_decision"] = "deferred"
        return deepcopy(self.job)

    def reject(self, job: dict[str, Any]) -> dict[str, Any]:
        self.job["status"] = "cancelled"
        return deepcopy(self.job)


def make_job() -> dict[str, Any]:
    return {
        "id": JOB_ID,
        "source_type": "youtube",
        "source_url": VIDEO_URL,
        "video_id": VIDEO_ID,
        "title": "테스트 영상",
        "tier": "T2",
        "status": "processing",
        "lease_token": "22222222-2222-4222-8222-222222222222",
        "source_guide": "",
        "result": {},
        "created_at": "2026-08-05T00:00:00Z",
    }


def test_youtube_url_normalization_removes_tracking() -> None:
    assert youtube_video_id(f"https://youtu.be/{VIDEO_ID}?si=tracking") == VIDEO_ID
    assert canonical_url(f"https://m.youtube.com/shorts/{VIDEO_ID}?utm_source=x") == VIDEO_URL
    assert canonical_url("https://Example.com/a?utm_source=x&b=2#frag") == "https://example.com/a?b=2"


def test_ytdlp_caption_command_never_uses_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    monkeypatch.setenv("FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak-either")
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "must-not-leak-uv")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("core.knowledge.subprocess.run", fake_run)

    run_ytdlp_caption_command(
        [sys.executable, "-m", "yt_dlp", "--skip-download", VIDEO_URL]
    )

    assert observed["kwargs"]["shell"] is False
    assert "FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY" not in observed["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in observed["kwargs"]["env"]
    assert "UV_PUBLISH_TOKEN" not in observed["kwargs"]["env"]
    assert observed["kwargs"]["check"] is False
    assert observed["args"][-1] == VIDEO_URL


def test_caption_provider_uses_only_public_captions_and_deletes_temp_dir() -> None:
    observed: dict[str, Any] = {}

    def fake_runner(
        args: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        observed["args"] = args
        observed["timeout"] = timeout
        template = Path(args[args.index("--output") + 1])
        observed["temp_root"] = template.parent
        (template.parent / "cap.ko.vtt").write_text(
            caption_vtt(),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    evidence = YoutubeCaptionProvider(fake_runner).fetch(
        f"{VIDEO_URL}&unsafe=$(touch hacked)"
    )

    assert evidence.evidence_hash == caption_evidence().evidence_hash
    assert observed["args"][0] == sys.executable
    assert observed["args"][-1] == VIDEO_URL
    assert "--skip-download" in observed["args"]
    assert "--ignore-config" in observed["args"]
    assert "--no-exec" in observed["args"]
    assert "--write-subs" in observed["args"]
    assert "--write-auto-subs" in observed["args"]
    assert "--extract-audio" not in observed["args"]
    assert observed["timeout"] == 180
    assert not observed["temp_root"].exists()


def test_caption_provider_failure_also_deletes_temp_dir() -> None:
    observed: dict[str, Path] = {}

    def fake_runner(
        args: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        template = Path(args[args.index("--output") + 1])
        observed["temp_root"] = template.parent
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="requested subtitles are not available",
        )

    with pytest.raises(CaptionEvidenceError) as raised:
        YoutubeCaptionProvider(fake_runner).fetch(VIDEO_URL)

    assert raised.value.code == "YTDLP_CAPTION_UNAVAILABLE"
    assert not observed["temp_root"].exists()


def test_caption_provider_rejects_oversized_vtt_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_runner(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        template = Path(args[args.index("--output") + 1])
        (template.parent / "cap.ko.vtt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def fail_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        raise AssertionError(f"oversized VTT must not be read: {self}")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    with pytest.raises(CaptionEvidenceError) as raised:
        YoutubeCaptionProvider(fake_runner).fetch(VIDEO_URL)

    assert raised.value.code == "YTDLP_CAPTION_FETCH_FAILED"


def test_caption_grounding_overwrites_fake_times_and_keeps_only_verified_short_quotes() -> None:
    grounded = grounded_draft()

    assert grounded["claims"][0]["citation"] == "[00:10]"
    assert grounded["claims"][0]["citation_verified"] is True
    assert grounded["coverage"]["middle"]["citation"] == "[02:30]"
    assert grounded["coverage"]["end"]["citation"] == "[04:50]"
    assert grounded["claims"][0]["evidence_quote"] == (
        "시작 구간에서 검증 가능한 사실을 자세히 설명합니다 지금"
    )
    assert grounded["coverage"]["middle"]["evidence_quote"] == (
        "중간 구간에서는 실행 방법과 중요한 제약을 설명합니다 지금"
    )
    assert "caption_quote" not in json.dumps(grounded, ensure_ascii=False)


def test_caption_grounding_rejects_hallucinated_or_short_quotes() -> None:
    hallucinated = valid_draft()
    hallucinated["claims"][0]["evidence_quote"] = "자막에 존재하지 않는 충분히 긴 환각 근거 문구입니다"
    with pytest.raises(KnowledgeError):
        ground_draft_with_caption_evidence(
            hallucinated,
            caption_evidence(),
        )

    too_short = valid_draft()
    too_short["claims"][0]["evidence_quote"] = "사실"
    with pytest.raises(KnowledgeError):
        ground_draft_with_caption_evidence(
            too_short,
            caption_evidence(),
        )


def test_caption_grounding_requires_start_middle_end_thirds() -> None:
    draft = valid_draft()
    draft["coverage"]["start"]["evidence_quote"] = (
        "마지막 구간에서는 결론과 다음 행동을 명확하게 정리합니다 지금"
    )

    with pytest.raises(KnowledgeError, match="start"):
        ground_draft_with_caption_evidence(
            draft,
            caption_evidence(),
        )


def test_caption_grounding_deoverlaps_rolling_youtube_cues() -> None:
    rolling = CaptionEvidence.from_vtt(
        """WEBVTT

00:00:01.000 --> 00:00:03.000
alpha beta gamma delta

00:00:03.000 --> 00:00:05.000
gamma delta epsilon zeta

00:00:05.000 --> 00:00:07.000
epsilon zeta eta theta iota
"""
    )

    assert rolling.locate(
        "beta gamma delta epsilon zeta eta theta iota"
    ) == 1.0


def test_caption_grounding_rejects_semantic_but_nonidentical_public_caption_match() -> None:
    evidence = CaptionEvidence.from_vtt(
        """WEBVTT

00:00:01.000 --> 00:00:03.000
the investment was about thirty billion dollars in total

00:00:03.000 --> 00:00:05.000
the team then changed the operating plan for customers

00:00:05.000 --> 00:00:07.000
the final recommendation was to review the evidence carefully
"""
    )

    with pytest.raises(KnowledgeError):
        evidence.locate(
            "the investment was around thirty billion dollars in total"
        )
    with pytest.raises(KnowledgeError):
        evidence.locate(
            "a completely unrelated statement about another market and company"
        )


def test_caption_index_resets_across_time_gaps_and_counts_duplicate_quotes() -> None:
    quote = "alpha beta gamma delta epsilon zeta eta theta"
    unique = CaptionEvidence(
        (
            CaptionCue(1.0, 2.0, quote),
            CaptionCue(1_000.0, 1_001.0, "unrelated later caption words remain separate"),
        ),
        "d" * 64,
        1_001.0,
    )
    assert unique.locate(quote) == 1.0

    split = CaptionEvidence(
        (
            CaptionCue(1.0, 2.0, "alpha beta gamma delta"),
            CaptionCue(1_000.0, 1_001.0, "epsilon zeta eta theta"),
        ),
        "e" * 64,
        1_001.0,
    )
    with pytest.raises(KnowledgeError):
        split.locate(quote)

    duplicate = CaptionEvidence(
        (
            CaptionCue(1.0, 2.0, quote),
            CaptionCue(1_000.0, 1_001.0, quote),
        ),
        "f" * 64,
        1_001.0,
    )
    with pytest.raises(KnowledgeError):
        duplicate.locate(quote)


@pytest.mark.parametrize(
    "caption, quote",
    [
        ("profit was −5 million dollars after taxes during fiscal year", "profit was 5 million dollars after taxes during fiscal year"),
        ("we must execute, John, after reviewing all eight source details", "we must execute John after reviewing all eight source details"),
    ],
)
def test_caption_grounding_preserves_semantic_punctuation(
    caption: str, quote: str,
) -> None:
    evidence = CaptionEvidence((CaptionCue(1.0, 2.0, caption),), "a" * 64, 2.0)
    with pytest.raises(KnowledgeError):
        evidence.locate(quote)


def test_caption_locate_many_reuses_prebuilt_token_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = knowledge_module._canonical_evidence_lexemes
    calls = 0

    def counted(value: str) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(knowledge_module, "_canonical_evidence_lexemes", counted)
    quotes = [f"claim {index} has eight stable caption source words today now" for index in range(67)]
    evidence = CaptionEvidence(
        tuple(CaptionCue(float(index), float(index + 1), quote) for index, quote in enumerate(quotes)),
        "b" * 64,
        67.0,
    )
    assert calls == len(quotes)
    calls = 0

    found = evidence.locate_many(quotes)

    assert len(found) == len(quotes)
    assert calls == len(quotes)


def test_candidate_bank_is_bounded_balanced_and_gap_safe() -> None:
    evidence = caption_evidence()
    bank = evidence.candidate_bank(caption_source_text(evidence))

    assert 3 <= len(bank) <= 36
    assert {candidate.part for candidate in bank} == {"start", "middle", "end"}
    assert all(8 <= len(knowledge_module._evidence_tokens(candidate.quote)) <= 15 for candidate in bank)
    assert len(json.dumps([candidate.prompt_payload() for candidate in bank], ensure_ascii=False).encode("utf-8")) <= 16 * 1024
    assert all(candidate.candidate_id.startswith({"start": "CS", "middle": "CM", "end": "CE"}[candidate.part]) for candidate in bank)


def test_candidate_bank_excludes_ambiguous_repeated_spans() -> None:
    repeated = "repeated evidence has eight stable exact caption words today"
    evidence = CaptionEvidence(
        (
            CaptionCue(1.0, 2.0, repeated),
            CaptionCue(40.0, 41.0, repeated),
            CaptionCue(80.0, 81.0, repeated),
        ),
        "9" * 64,
        90.0,
    )

    with pytest.raises(KnowledgeError, match="no unique"):
        evidence.candidate_bank(caption_source_text(evidence))


def test_candidate_bank_excludes_caption_only_spans_before_query() -> None:
    cues = (
        CaptionCue(1.0, 2.0, "caption only start evidence has eight stable words today"),
        CaptionCue(10.0, 11.0, "dual grounded start evidence has eight stable words today"),
        CaptionCue(40.0, 41.0, "caption only middle evidence has eight stable words today"),
        CaptionCue(50.0, 51.0, "dual grounded middle evidence has eight stable words today"),
        CaptionCue(80.0, 81.0, "caption only ending evidence has eight stable words today"),
        CaptionCue(90.0, 91.0, "dual grounded ending evidence has eight stable words today"),
    )
    evidence = CaptionEvidence(cues, "8" * 64, 100.0)
    source = " ".join(cue.text for cue in (cues[1], cues[3], cues[5]))

    bank = evidence.candidate_bank(source)

    assert {candidate.part for candidate in bank} == {"start", "middle", "end"}
    assert all(
        knowledge_module._source_quote_matches_bounded_window(
            source, candidate.quote, candidate.timestamp, evidence.duration_seconds
        )
        for candidate in bank
    )
    with pytest.raises(KnowledgeError, match="no unique end"):
        evidence.candidate_bank(" ".join(cue.text for cue in (cues[1], cues[3])))


def test_candidate_contract_rejects_unknown_cross_part_duplicate_and_quote_fields() -> None:
    bank = caption_evidence().candidate_bank(transcript())
    prompt = knowledge_module.build_candidate_query_prompt(make_job(), bank)
    draft = candidate_contract_response(prompt, valid_draft())

    unknown = deepcopy(draft)
    unknown["claims"][0]["evidence_id"] = "CS99"
    with pytest.raises(KnowledgeError, match="unknown"):
        knowledge_module._hydrate_candidate_draft(unknown, bank)

    cross_part = deepcopy(draft)
    cross_part["coverage"]["start"]["evidence_id"] = next(item.candidate_id for item in bank if item.part == "middle")
    with pytest.raises(KnowledgeError, match="another part"):
        knowledge_module._hydrate_candidate_draft(cross_part, bank)

    duplicate = deepcopy(draft)
    duplicate["claims"].append(deepcopy(duplicate["claims"][0]))
    with pytest.raises(KnowledgeError, match="duplicate"):
        knowledge_module._hydrate_candidate_draft(duplicate, bank)

    fabricated = deepcopy(draft)
    fabricated["claims"][0]["evidence_quote"] = "fabricated raw quote must never be accepted"
    with pytest.raises(KnowledgeError, match="forbidden"):
        knowledge_module._hydrate_candidate_draft(fabricated, bank)

    model_owned_flag = deepcopy(draft)
    model_owned_flag["claims"][0]["requires_crosscheck"] = False
    with pytest.raises(KnowledgeError, match="forbidden"):
        knowledge_module._hydrate_candidate_draft(model_owned_flag, bank)

    expanded = deepcopy(draft)
    expanded["claims"][0]["statement"] = "This tool is growing rapidly without evidence."
    hydrated, semantic_items = knowledge_module._hydrate_candidate_draft(expanded, bank)
    selected_quote = next(item.quote for item in bank if item.candidate_id == expanded["claims"][0]["evidence_id"])
    assert hydrated["claims"][0]["statement"] == selected_quote
    assert semantic_items[0]["statement"] == expanded["claims"][0]["statement"]

    tautological = deepcopy(draft)
    tautological["claims"][0]["statement"] = selected_quote
    hydrated_tautology, tautological_items = knowledge_module._hydrate_candidate_draft(
        tautological, bank
    )
    assert hydrated_tautology["claims"][0]["requires_crosscheck"] is True
    assert not any(item["item_id"].startswith("F") for item in tautological_items)


def test_candidate_contract_discards_valid_non_fact_evidence_id() -> None:
    bank = caption_evidence().candidate_bank(transcript())
    prompt = knowledge_module.build_candidate_query_prompt(make_job(), bank)
    draft = candidate_contract_response(prompt, valid_draft())
    non_fact = {
        "type": "recommendation",
        "statement": "Keep this recommendation subject to human judgment.",
        "evidence_id": bank[0].candidate_id,
    }
    draft["claims"].append(non_fact)

    hydrated, semantic_items = knowledge_module._hydrate_candidate_draft(draft, bank)

    hydrated_non_fact = next(
        claim for claim in hydrated["claims"] if claim["type"] == non_fact["type"]
    )
    assert "evidence_id" not in hydrated_non_fact
    assert hydrated_non_fact["evidence_quote"] == ""
    assert hydrated_non_fact["requires_crosscheck"] is False
    assert all(item["statement"] != non_fact["statement"] for item in semantic_items)

    non_fact["evidence_id"] = "CS99"
    with pytest.raises(KnowledgeError, match="unknown"):
        knowledge_module._hydrate_candidate_draft(draft, bank)


def test_candidate_selection_format_retries_once_then_runs_semantic_evaluator(
    tmp_path: Path,
) -> None:
    class SelectionRetryRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                prompt = args[3]
                if "EVIDENCE_CANDIDATES=" in prompt and self.query_calls == 1:
                    self.calls.append(args)
                    response = candidate_contract_response(prompt, valid_draft())
                    response["claims"][0]["evidence_id"] = "CS01, CM01"
                    return json.dumps({"answer": json.dumps(response, ensure_ascii=False)})
            return super().__call__(args, timeout)

    runner = SelectionRetryRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert runner.query_calls == 3


def test_candidate_selection_format_retry_is_bounded_and_fail_closed(
    tmp_path: Path,
) -> None:
    class RepeatedInvalidSelectionRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                prompt = args[3]
                if "EVIDENCE_CANDIDATES=" in prompt:
                    self.calls.append(args)
                    response = candidate_contract_response(prompt, valid_draft())
                    response["claims"][0]["evidence_id"] = "CS01, CM01"
                    return json.dumps({"answer": json.dumps(response, ensure_ascii=False)})
            return super().__call__(args, timeout)

    runner = RepeatedInvalidSelectionRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "NLM_DRAFT_CONTRACT_INVALID"
    assert runner.query_calls == 2
    with pytest.raises(KnowledgeError, match="retry prompt exceeds"):
        knowledge_module.build_candidate_selection_retry_prompt("x" * 32_700)


def test_candidate_and_semantic_prompts_are_hard_capped() -> None:
    bank = caption_evidence().candidate_bank(transcript())
    candidate_prompt = knowledge_module.build_candidate_query_prompt(make_job(), bank)
    assert len(candidate_prompt.encode("utf-8")) <= 32 * 1024

    oversized_job = make_job()
    oversized_job["title"] = "x" * (33 * 1024)
    with pytest.raises(KnowledgeError, match="prompt exceeds"):
        knowledge_module.build_candidate_query_prompt(oversized_job, bank)

    oversized_items = ({
        "item_id": "F01", "candidate_id": "CS01",
        "quote": "q" * (17 * 1024), "statement": "s" * (17 * 1024),
    },)
    with pytest.raises(KnowledgeError, match="prompt exceeds"):
        knowledge_module.build_semantic_evaluator_prompt(oversized_items)


@pytest.mark.parametrize("mode", ["unknown", "cross_part", "fabricated", "duplicate"])
def test_candidate_contract_failure_does_not_query_semantic_evaluator(
    tmp_path: Path, mode: str,
) -> None:
    class InvalidCandidateRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                prompt = args[3]
                if "EVIDENCE_CANDIDATES=" in prompt:
                    self.calls.append(args)
                    response = candidate_contract_response(prompt, valid_draft())
                    if mode == "unknown":
                        response["claims"][0]["evidence_id"] = "CS99"
                    elif mode == "cross_part":
                        response["coverage"]["start"]["evidence_id"] = response["coverage"]["middle"]["evidence_id"]
                    elif mode == "fabricated":
                        response["claims"][0]["evidence_quote"] = "invented paraphrase"
                    elif mode == "duplicate":
                        response["claims"].append(deepcopy(response["claims"][0]))
                    return json.dumps({"answer": json.dumps(response, ensure_ascii=False)})
            return super().__call__(args, timeout)

    runner = InvalidCandidateRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "NLM_DRAFT_CONTRACT_INVALID"
    assert runner.query_calls == 1


@pytest.mark.parametrize("mode", ["false", "missing", "extra", "mutate", "malformed"])
def test_semantic_verdict_failures_are_action_required_without_retry(
    tmp_path: Path, mode: str,
) -> None:
    class VerdictRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                prompt = args[3]
                if prompt.startswith("Verdict-only semantic check"):
                    self.calls.append(args)
                    if mode == "malformed":
                        return json.dumps({"answer": "not json"})
                    verdict = semantic_verdict_response(prompt, supported=mode != "false")
                    if mode == "missing":
                        verdict["items"].pop()
                    elif mode == "extra":
                        verdict["items"].append({"id": "EXTRA", "supported": True})
                    elif mode == "mutate":
                        verdict["items"][0]["quote"] = "evaluator attempted mutation"
                    return json.dumps({"answer": json.dumps(verdict)})
            return super().__call__(args, timeout)

    runner = VerdictRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "NLM_EVIDENCE_NOT_SUPPORTED"
    assert runner.query_calls == 2
    assert not (tmp_path / "reviews" / f"{JOB_ID}.json").exists()


def test_candidate_semantic_success_is_human_approval_ready_with_visible_limitation(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)
    reviews = service.reviews()

    assert report["review_required"] == [JOB_ID]
    assert len([call for call in runner.calls if call[:2] == ["notebook", "query"]]) == 2
    facts = [claim for claim in queue.job["result"]["draft"]["claims"] if claim["type"] == "fact"]
    assert facts and all(claim["requires_crosscheck"] is False for claim in facts)
    assert queue.job["quality_report"]["semantic_evaluator"] == "notebooklm-second-pass-semantic-consistency-v1"
    assert queue.job["quality_report"]["semantic_evaluator_independent"] is False
    assert any("독립 모델 검증이 아닙니다" in warning for warning in reviews["items"][0]["qualityWarnings"])
    assert reviews["items"][0]["approvalReady"] is True


def test_tautological_fact_quote_remains_human_crosscheck_required(
    tmp_path: Path,
) -> None:
    class TautologicalFactRunner(FakeRunner):
        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"] and "EVIDENCE_CANDIDATES=" in args[3]:
                self.calls.append(args)
                response = candidate_contract_response(args[3], valid_draft())
                candidates = json.loads(
                    next(
                        line for line in args[3].splitlines()
                        if line.startswith("EVIDENCE_CANDIDATES=")
                    ).split("=", 1)[1]
                )
                by_id = {item["id"]: item for item in candidates}
                fact = next(claim for claim in response["claims"] if claim["type"] == "fact")
                fact["statement"] = by_id[fact["evidence_id"]]["quote"]
                return json.dumps({"answer": json.dumps(response, ensure_ascii=False)})
            return super().__call__(args, timeout)

    runner = TautologicalFactRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["review_required"] == [JOB_ID]
    fact = next(
        claim for claim in queue.job["result"]["draft"]["claims"]
        if claim["type"] == "fact"
    )
    assert fact["requires_crosscheck"] is True
    semantic_prompt = next(
        call[3] for call in runner.calls
        if call[:2] == ["notebook", "query"]
        and call[3].startswith("Verdict-only semantic check")
    )
    assert '"item_id":"F' not in semantic_prompt


def test_caption_grounding_rejects_changed_numbers_negation_and_ambiguous_matches() -> None:
    numeric = CaptionEvidence.from_vtt(
        """WEBVTT

00:00:01.000 --> 00:00:03.000
the investment was about thirty billion dollars in total

00:00:03.000 --> 00:00:05.000
the team did not approve the final operating plan

00:00:05.000 --> 00:00:07.000
the final recommendation was to review the evidence carefully
"""
    )
    with pytest.raises(KnowledgeError):
        numeric.locate("the investment was about thirteen billion dollars in total")
    with pytest.raises(KnowledgeError):
        numeric.locate("the team did approve the final operating plan")

    named = CaptionEvidence.from_vtt(
        """WEBVTT

00:00:01.000 --> 00:00:03.000
the report says Alice approved the operating plan today

00:00:03.000 --> 00:00:05.000
the team then documented every important supporting detail

00:00:05.000 --> 00:00:07.000
the final recommendation was to review the evidence carefully
"""
    )
    with pytest.raises(KnowledgeError):
        named.locate("the report says Bob approved the operating plan today")

    ambiguous = CaptionEvidence.from_vtt(
        """WEBVTT

00:00:01.000 --> 00:00:03.000
review the complete evidence before making the final decision today

00:00:10.000 --> 00:00:12.000
review the complete evidence before making the final decision today

00:00:20.000 --> 00:00:22.000
the closing section contains a different and sufficiently long sentence
"""
    )
    with pytest.raises(KnowledgeError):
        ambiguous.locate("review the complete evidence before making the final decision today")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda draft: draft.update({"caption_text": "sensitive raw caption"}),
        lambda draft: draft.update({"transcript": "sensitive raw transcript"}),
        lambda draft: draft["claims"][0].update({"raw_source": "sensitive source"}),
        lambda draft: draft["coverage"]["start"].update({"caption_text": "sensitive caption"}),
    ],
)
def test_draft_contract_rejects_unknown_fields_before_persistence(mutate: Any) -> None:
    draft = valid_draft()
    mutate(draft)

    with pytest.raises(KnowledgeError, match="허용되지 않은"):
        ground_draft_with_caption_evidence(draft, caption_evidence(), transcript())


def test_process_never_persists_unknown_model_fields(tmp_path: Path) -> None:
    malicious = valid_draft()
    malicious["caption_text"] = "sensitive raw caption body"
    queue = FakeQueue()
    reviews = ReviewStore(tmp_path / "reviews")
    service = KnowledgeService(
        NotebookLmClient(FakeRunner(draft=malicious)),
        NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"]),
        queue=queue,
        review_store=reviews,
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]', "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["action_required"] == [JOB_ID]
    persisted = json.dumps(queue.job, ensure_ascii=False)
    assert "sensitive raw caption body" not in persisted
    assert "caption_text" not in persisted
    assert queue.job["failure_code"] == "NLM_DRAFT_CONTRACT_INVALID"
    assert not (tmp_path / "reviews" / f"{JOB_ID}.json").exists()


def test_inventory_reports_cross_notebook_duplicate_without_deleting(tmp_path: Path) -> None:
    runner = FakeRunner(duplicate=True)
    registry = NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"])
    service = KnowledgeService(
        NotebookLmClient(runner),
        registry,
        env={"KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]'},
    )

    result = service.inventory(force=True)

    assert result["source_count"] == 2
    assert len(result["duplicate_groups"]) == 1
    assert result["duplicate_groups"][0]["preferred"]["notebook_id"] == "nb1"
    assert result["duplicate_groups"][0]["duplicates"][0]["notebook_id"] == "nb2"
    assert result["duplicate_groups"][0]["auto_deleted"] is False


def test_inventory_keeps_sources_when_notebooklm_omits_urls(tmp_path: Path) -> None:
    class NullUrlRunner:
        def __call__(self, args: list[str], timeout: int) -> str:
            del timeout
            if args[:2] == ["notebook", "list"]:
                return json.dumps({"notebooks": [{"id": "nb1", "title": "Existing"}]})
            if args[:2] == ["source", "list"]:
                return json.dumps(
                    {
                        "sources": [
                            {
                                "id": "youtube-source",
                                "title": "Existing video",
                                "type": "youtube",
                                "url": None,
                                "status": 2,
                            },
                            {
                                "id": "pdf-source",
                                "title": "Existing PDF",
                                "type": "pdf",
                                "url": None,
                                "status": 2,
                            },
                        ]
                    }
                )
            raise AssertionError(f"unexpected command: {args}")

    service = KnowledgeService(
        NotebookLmClient(NullUrlRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
    )

    result = service.inventory(force=True)

    assert result["source_count"] == 2
    assert result["youtube_source_count"] == 1
    assert result["unresolved_url_source_count"] == 2
    assert result["unresolved_youtube_source_count"] == 1
    assert result["manual_preadded_video_id_reuse_complete"] is False
    assert result["duplicate_groups"] == []


def test_registry_refresh_preserves_known_url_when_cli_omits_it(tmp_path: Path) -> None:
    class NullUrlRunner:
        def __call__(self, args: list[str], timeout: int) -> str:
            del timeout
            if args[:2] == ["notebook", "list"]:
                return json.dumps({"notebooks": [{"id": "nb1", "title": "Existing"}]})
            if args[:2] == ["source", "list"]:
                return json.dumps(
                    {
                        "sources": [
                            {
                                "id": "source-nb1",
                                "title": "Existing video",
                                "type": "youtube",
                                "url": None,
                                "status": 2,
                            }
                        ]
                    }
                )
            raise AssertionError(f"unexpected command: {args}")

    registry = NotebookRegistry(tmp_path / "registry.json")
    registry.save(
        RegistrySnapshot(
            "2026-08-05T00:00:00Z",
            [
                SourceInfo(
                    "nb1",
                    "Existing",
                    "source-nb1",
                    "Existing video",
                    VIDEO_URL,
                    "2026-08-05T00:00:00Z",
                    source_type="youtube",
                )
            ],
        )
    )

    snapshot = registry.refresh(NotebookLmClient(NullUrlRunner()))

    assert snapshot.sources[0].url == VIDEO_URL
    assert registry.find(VIDEO_URL)[0].source_id == "source-nb1"


def test_process_stops_before_add_when_unresolved_youtube_title_matches(
    tmp_path: Path,
) -> None:
    class ExistingTitleRunner:
        def __init__(self) -> None:
            self.add_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            del timeout
            if args[:2] == ["notebook", "list"]:
                return json.dumps({"notebooks": [{"id": "nb1", "title": "Existing"}]})
            if args[:2] == ["source", "list"]:
                return json.dumps(
                    {
                        "sources": [
                            {
                                "id": "unresolved-source",
                                "title": make_job()["title"],
                                "type": "youtube",
                                "url": None,
                                "status": 2,
                            }
                        ]
                    }
                )
            if args[:2] == ["source", "add"]:
                self.add_calls += 1
                raise AssertionError("source add must not run for an exact-title unresolved candidate")
            raise AssertionError(f"unexpected command: {args}")

    runner = ExistingTitleRunner()
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1", "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    result = service.process(limit=1)

    assert result["action_required"] == [JOB_ID]
    assert runner.add_calls == 0
    assert queue.job["failure_code"] == "NOTEBOOKLM_SOURCE_IDENTITY_REQUIRED"


@pytest.mark.parametrize(
    "env",
    [
        {
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "100",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
        {
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
        {
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
            "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "not-a-number",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
    ],
)
def test_new_source_add_fails_closed_without_allowlist_and_positive_limit(
    tmp_path: Path,
    env: dict[str, str],
) -> None:
    class EmptyRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                self.calls.append(args)
                return json.dumps({"sources": []})
            if args[:2] == ["source", "add"]:
                self.add_calls += 1
            return super().__call__(args, timeout)

    runner = EmptyRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        caption_provider=FakeCaptionProvider(),
        env=env,
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert runner.add_calls == 0


def test_source_add_partial_success_is_reconciled_without_duplicate(
    tmp_path: Path,
) -> None:
    class PartialSuccessRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0
            self.added = False

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                self.calls.append(args)
                sources = []
                if self.added:
                    sources.append(
                        {
                            "id": "source-added-after-error",
                            "title": VIDEO_URL,
                            "url": None,
                            "type": "youtube",
                            "status": 3,
                        }
                    )
                return json.dumps({"sources": sources})
            if args[:2] == ["source", "add"]:
                self.calls.append(args)
                self.add_calls += 1
                self.added = True
                raise knowledge_module.NotebookLmCommandError(
                    "NLM_PROCESSING_FAILED",
                    "NotebookLM source add returned an error after creating the source.",
                )
            return super().__call__(args, timeout)

    runner = PartialSuccessRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
            "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "100",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
    ).process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert report["action_required"] == []
    assert runner.add_calls == 1
    assert queue.job["notebook_id"] == "nb1"
    assert queue.job["notebook_source_id"] == "source-added-after-error"


def test_source_add_error_without_exact_reconciliation_never_readds(
    tmp_path: Path,
) -> None:
    class UncertainAddRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0
            self.added = False

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                self.calls.append(args)
                sources = []
                if self.added:
                    sources.append(
                        {
                            "id": "source-without-url-after-error",
                            "title": "Unresolved YouTube source",
                            "url": None,
                            "type": "youtube",
                            "status": 2,
                        }
                    )
                return json.dumps({"sources": sources})
            if args[:2] == ["source", "add"]:
                self.calls.append(args)
                self.add_calls += 1
                self.added = True
                raise knowledge_module.NotebookLmCommandError(
                    "NLM_PROCESSING_FAILED",
                    "NotebookLM source add outcome is unknown.",
                )
            return super().__call__(args, timeout)

    runner = UncertainAddRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
            "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "100",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
    ).process(limit=1)

    assert report["review_required"] == []
    assert report["action_required"] == [JOB_ID]
    assert runner.add_calls == 1
    assert queue.job["failure_code"] == "NOTEBOOKLM_SOURCE_IDENTITY_REQUIRED"
    assert queue.job.get("notebook_source_id") is None


def test_source_add_error_without_side_effect_preserves_original_failure(
    tmp_path: Path,
) -> None:
    class NoMutationRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                self.calls.append(args)
                return json.dumps({"sources": []})
            if args[:2] == ["source", "add"]:
                self.calls.append(args)
                self.add_calls += 1
                raise knowledge_module.NotebookLmCommandError(
                    "NOTEBOOKLM_AUTH_REQUIRED",
                    "NotebookLM authentication is required.",
                )
            return super().__call__(args, timeout)

    runner = NoMutationRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        caption_provider=FakeCaptionProvider(),
        env={
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
            "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "100",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert runner.add_calls == 1
    assert queue.job["failure_code"] == "NOTEBOOKLM_AUTH_REQUIRED"


def test_source_identity_is_checkpointed_before_registry_append(
    tmp_path: Path,
) -> None:
    class EmptyRunner(FakeRunner):
        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                self.calls.append(args)
                return json.dumps({"sources": []})
            return super().__call__(args, timeout)

    class FailingAppendRegistry(NotebookRegistry):
        def append(self, source: SourceInfo) -> None:
            del source
            raise OSError("registry write failed")

    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(EmptyRunner()),
        FailingAppendRegistry(tmp_path / "registry.json"),
        queue=queue,
        caption_provider=FakeCaptionProvider(),
        env={
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
            "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "100",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["notebook_id"] == "nb1"
    assert queue.job["notebook_source_id"] == "source-added"


def test_checkpointed_source_is_reused_and_never_readded(tmp_path: Path) -> None:
    job = make_job()
    job.update({"notebook_id": "nb1", "notebook_source_id": "source-nb1"})
    runner = FakeRunner()
    report = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=FakeQueue(job),
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert not any(call[:2] == ["source", "add"] for call in runner.calls)


def test_missing_checkpointed_source_fails_closed_without_add(tmp_path: Path) -> None:
    class MissingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                self.calls.append(args)
                return json.dumps({"sources": []})
            if args[:2] == ["source", "add"]:
                self.add_calls += 1
                raise AssertionError("checkpointed source must never be re-added")
            return super().__call__(args, timeout)

    job = make_job()
    job.update({"notebook_id": "nb1", "notebook_source_id": "source-nb1"})
    runner = MissingRunner()
    queue = FakeQueue(job)
    report = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "NOTEBOOKLM_SOURCE_IDENTITY_REQUIRED"
    assert runner.add_calls == 0


def test_registry_prefers_latest_non_archive_when_no_canonical_source_exists(tmp_path: Path) -> None:
    registry = NotebookRegistry(tmp_path / "registry.json")
    registry.save(
        RegistrySnapshot(
            "2026-08-05T02:00:00Z",
            [
                SourceInfo("nb-old", "YT · Misc", "source-old", "old", VIDEO_URL, "2026-08-05T00:00:00Z"),
                SourceInfo("nb-new", "YT · Misc", "source-new", "new", VIDEO_URL, "2026-08-05T01:00:00Z"),
                SourceInfo("nb-archive", "ARCHIVE · YT", "source-archive", "archive", VIDEO_URL, "2026-08-05T02:00:00Z"),
            ],
        )
    )

    assert [item.source_id for item in registry.find(VIDEO_URL)] == [
        "source-new",
        "source-old",
        "source-archive",
    ]


def test_registry_never_prefers_archive_or_unready_source_only_because_it_is_allowlisted(
    tmp_path: Path,
) -> None:
    registry = NotebookRegistry(
        tmp_path / "registry.json",
        canonical_notebook_ids=["nb-archive", "nb-unready", "nb-standard"],
    )
    registry.save(
        RegistrySnapshot(
            "2026-08-05T03:00:00Z",
            [
                SourceInfo(
                    "nb-archive",
                    "ARCHIVE · YT",
                    "source-archive",
                    "archive",
                    VIDEO_URL,
                    "2026-08-05T03:00:00Z",
                    first_seen_at="2026-08-05T03:00:00Z",
                    status=2,
                ),
                SourceInfo(
                    "nb-unready",
                    "YT · Inbox",
                    "source-unready",
                    "unready",
                    VIDEO_URL,
                    "2026-08-05T03:00:00Z",
                    first_seen_at="2026-08-05T03:00:00Z",
                    status=1,
                ),
                SourceInfo(
                    "nb-standard",
                    "YT · 미분류 · Inbox",
                    "source-standard",
                    "standard",
                    VIDEO_URL,
                    "2026-08-05T01:00:00Z",
                    first_seen_at="2026-08-05T01:00:00Z",
                    status=2,
                ),
            ],
        )
    )

    assert [item.source_id for item in registry.find(VIDEO_URL)] == [
        "source-standard",
        "source-archive",
        "source-unready",
    ]


def test_registry_refresh_preserves_first_seen_and_content_hash(tmp_path: Path) -> None:
    registry = NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"])
    registry.save(
        RegistrySnapshot(
            "2026-08-04T00:00:00Z",
            [
                SourceInfo(
                    "nb1",
                    "YT · 미분류 · Inbox",
                    "source-nb1",
                    "기존 영상",
                    VIDEO_URL,
                    "2026-08-04T00:00:00Z",
                    content_hash="abc123",
                    first_seen_at="2026-08-01T00:00:00Z",
                    status=2,
                )
            ],
        )
    )

    snapshot = registry.refresh(
        NotebookLmClient(FakeRunner()),
        now=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )

    source = snapshot.sources[0]
    assert source.checked_at == "2026-08-05T00:00:00Z"
    assert source.first_seen_at == "2026-08-01T00:00:00Z"
    assert source.content_hash == "abc123"
    assert source.status == 2


def test_service_uses_only_default_notebook_as_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_NOTEBOOK_DEFAULT_ID", "nb-standard")
    monkeypatch.setenv("KNOWLEDGE_NOTEBOOK_ALLOWLIST", '["nb-archive", "nb-standard", "nb-extra"]')

    service = KnowledgeService.from_env()

    assert service.registry.canonical_notebook_ids == ("nb-standard",)


def test_two_workers_add_same_notebooklm_source_only_once(tmp_path: Path) -> None:
    runner = ConcurrentAddRunner()
    registry_path = tmp_path / "registry.json"

    def run_worker(index: int) -> dict[str, Any]:
        return KnowledgeService(
            NotebookLmClient(runner),
            NotebookRegistry(registry_path, canonical_notebook_ids=["nb1"]),
            queue=FakeQueue(),
            review_store=ReviewStore(tmp_path / f"reviews-{index}"),
            caption_provider=FakeCaptionProvider(),
            env={
                "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
                "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
                "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "100",
                "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
            },
        ).process(limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(run_worker, range(2)))

    assert runner.add_calls == 1
    assert all(report["review_required"] == [JOB_ID] for report in reports)


def test_process_reuses_existing_source_and_queries_only_that_source(tmp_path: Path) -> None:
    runner = FakeRunner()
    queue = FakeQueue()
    registry = NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"])
    service = KnowledgeService(
        NotebookLmClient(runner),
        registry,
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=9)

    assert report["requested_limit"] == 3
    assert report["review_required"] == [JOB_ID]
    assert "review_required" in queue.completions
    assert not any(call[:2] == ["source", "add"] for call in runner.calls)
    query = next(call for call in runner.calls if call[:2] == ["notebook", "query"])
    assert query[query.index("--source-ids") + 1] == "source-nb1"
    assert (tmp_path / "reviews" / f"{JOB_ID}.json").exists()
    assert (tmp_path / "transcripts" / f"{JOB_ID}.txt").read_text(encoding="utf-8") == transcript()
    assert registry.find(VIDEO_URL)[0].content_hash == queue.job["source_hash"]
    assert queue.job["transcript_hash"] == caption_evidence().evidence_hash
    persisted = json.dumps(queue.job, ensure_ascii=False)
    assert queue.job["result"]["draft"]["claims"][0]["evidence_quote"] == (
        "시작 구간에서 검증 가능한 사실을 자세히 설명합니다 지금"
    )
    assert "caption_quote" not in persisted
    assert not any(
        _TIMESTAMP_PATTERN
        for _TIMESTAMP_PATTERN in ["00:01", "10:00", "20:00"]
        if _TIMESTAMP_PATTERN in transcript()
    )


def test_exact_process_claims_only_requested_job_after_clean_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = make_job()
    job.update(
        {
            "status": "queued",
            "attempt_count": 0,
            "capture_ready": True,
            "lease_token": None,
            "metadata": {"_canary_hold": True, "_canary_no_retry": True},
        }
    )
    queue = FakeQueue(job)
    monkeypatch.setattr(
        knowledge_module,
        "runtime_git_provenance",
        lambda expected_sha=None, require_clean=False: {
            "git_sha": expected_sha or "a" * 40,
            "tracked_clean": require_clean,
            "module_path": __file__,
            "repository_root": str(tmp_path),
        },
    )
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"]),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(job_id=JOB_ID, expected_git_sha="a" * 40)

    assert report["exact_job_id"] == JOB_ID
    assert report["requested_limit"] == 1
    assert report["review_required"] == [JOB_ID]
    assert queue.job["attempt_count"] == 1


def test_runtime_git_provenance_rejects_invalid_sha_before_running_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("git must not run for an invalid expected SHA")

    monkeypatch.setattr(knowledge_module.subprocess, "run", unexpected_run)

    with pytest.raises(KnowledgeError, match="full 40-character"):
        knowledge_module.runtime_git_provenance("short", require_clean=True)


@pytest.mark.parametrize(
    ("head", "status", "expected", "message"),
    [
        ("a" * 40, "", "b" * 40, "does not match"),
        ("a" * 40, " M core/knowledge.py", "a" * 40, "uncommitted tracked changes"),
    ],
)
def test_runtime_git_provenance_rejects_mismatch_and_dirty_tree(
    monkeypatch: pytest.MonkeyPatch,
    head: str,
    status: str,
    expected: str,
    message: str,
) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        output = head if command[1:3] == ["rev-parse", "HEAD"] else status
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(knowledge_module.subprocess, "run", fake_run)

    with pytest.raises(KnowledgeError, match=message):
        knowledge_module.runtime_git_provenance(expected, require_clean=True)


def test_doctor_is_read_only_and_reports_that_execution_must_repeat_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = make_job()
    job.update({"status": "queued", "attempt_count": 0, "capture_ready": True, "lease_token": None})
    queue = FakeQueue(job)
    monkeypatch.setattr(
        knowledge_module,
        "runtime_git_provenance",
        lambda expected_sha=None, require_clean=False: {
            "git_sha": expected_sha or "a" * 40,
            "tracked_clean": not require_clean,
            "module_path": __file__,
            "repository_root": str(tmp_path),
        },
    )
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.doctor(JOB_ID)

    assert report["read_only"] is True
    assert report["read_only_scope"] == "queue-and-external-sources"
    assert report["local_registry_cache_may_refresh"] is True
    assert report["execution_safety_guaranteed"] is False
    assert report["ready_state"] is True
    assert queue.claimed is False
    assert queue.job["attempt_count"] == 0


def test_exact_process_reuses_preflight_caption_for_case_normalized_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = make_job()
    job.update({"status": "queued", "attempt_count": 0, "capture_ready": True, "lease_token": None})

    class CaseNormalizingQueue(FakeQueue):
        def claim_exact(self, job_id: str, worker_id: str) -> dict[str, Any]:
            assert job_id.lower() == self.job["id"]
            return super().claim_exact(job_id.lower(), worker_id)

    queue = CaseNormalizingQueue(job)
    captions = FakeCaptionProvider()
    monkeypatch.setattr(
        knowledge_module,
        "runtime_git_provenance",
        lambda expected_sha=None, require_clean=False: {
            "git_sha": expected_sha or "a" * 40,
            "tracked_clean": require_clean,
            "module_path": __file__,
            "repository_root": str(tmp_path),
        },
    )
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"]),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=captions,
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(job_id=JOB_ID.upper(), expected_git_sha="a" * 40)

    assert report["review_required"] == [JOB_ID]
    assert captions.calls == [VIDEO_URL]


def test_exact_process_rejects_bad_provenance_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = make_job()
    job.update({"status": "queued", "attempt_count": 0, "capture_ready": True, "lease_token": None})
    queue = FakeQueue(job)
    monkeypatch.setattr(
        knowledge_module,
        "runtime_git_provenance",
        lambda expected_sha=None, require_clean=False: (_ for _ in ()).throw(
            KnowledgeError("knowledge runtime has uncommitted tracked changes")
        ),
    )
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    with pytest.raises(KnowledgeError, match="uncommitted tracked changes"):
        service.process(job_id=JOB_ID, expected_git_sha="a" * 40)

    assert queue.claimed is False


def test_exact_process_rejects_terminal_claim_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = make_job()
    job.update({"status": "queued", "attempt_count": 2, "capture_ready": True, "lease_token": None})

    class TerminalExactQueue(FakeQueue):
        def claim_exact(self, job_id: str, worker_id: str) -> dict[str, Any]:
            assert job_id == self.job["id"]
            assert worker_id.startswith("knowledge-worker:")
            self.claimed = True
            self.job.update(
                {
                    "status": "action_required",
                    "failure_code": "CANARY_LEASE_EXPIRED",
                    "failure_message": "Clean canary lease expired; retry is disabled.",
                    "lease_token": None,
                }
            )
            return deepcopy(self.job)

    queue = TerminalExactQueue(job)
    monkeypatch.setattr(
        knowledge_module,
        "runtime_git_provenance",
        lambda expected_sha=None, require_clean=False: {
            "git_sha": expected_sha or "a" * 40,
            "tracked_clean": require_clean,
            "module_path": __file__,
            "repository_root": str(tmp_path),
        },
    )
    runner = FakeRunner()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    with pytest.raises(KnowledgeError, match="did not acquire a processing lease"):
        service.process(job_id=JOB_ID, expected_git_sha="a" * 40)

    assert queue.claimed is True
    assert not any(command[:2] == ["notebook", "query"] for command in runner.calls)


def test_process_refuses_to_claim_without_public_caption_opt_in(tmp_path: Path) -> None:
    runner = FakeRunner()
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"]),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        env={},
    )

    with pytest.raises(
        KnowledgeError,
        match="Public-caption processing is disabled",
    ):
        service.process()

    assert queue.claimed is False
    assert runner.calls == []


def test_source_get_grounding_verifies_distinct_source_thirds_but_not_timestamps() -> None:
    grounded = ground_draft_with_source_evidence(valid_draft(), transcript(), "source-nb1")

    citations = [grounded["coverage"][part]["citation"] for part in ("start", "middle", "end")]
    positions = [int(citation.rsplit("=", 1)[1]) for citation in citations]
    assert positions == sorted(positions)
    assert len(set(positions)) == 3
    quality = evaluate_draft(
        grounded,
        transcript_evidence=True,
        evidence_contract="notebooklm-source-get-v1",
    )
    assert quality["passed"] is False
    assert quality["dimensions"]["coverage"] == 0


def test_source_get_grounding_rejects_wrong_source_third() -> None:
    draft = valid_draft()
    draft["coverage"]["start"]["evidence_quote"] = draft["coverage"]["end"]["evidence_quote"]

    with pytest.raises(KnowledgeError, match="start"):
        ground_draft_with_source_evidence(draft, transcript(), "source-nb1")


def test_transcript_provider_is_fail_closed_without_explicit_flag(tmp_path: Path) -> None:
    provider = FakeCaptionProvider(
        error=CaptionEvidenceError("YTDLP_CAPTION_UNAVAILABLE", "must not be called")
    )
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"]),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=provider, env={},
    )

    with pytest.raises(
        KnowledgeError,
        match="Public-caption processing is disabled",
    ):
        service.process()

    assert provider.calls == []
    assert queue.claimed is False


def test_source_get_extracts_content_from_cli_json() -> None:
    runner = lambda args, timeout: json.dumps({"title": "영상", "content": transcript()}, ensure_ascii=False)
    assert NotebookLmClient(runner).get_source("source-1") == transcript().strip()


def test_caption_failure_stops_before_source_get_query_or_add(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    queue = FakeQueue()
    provider = FakeCaptionProvider(
        error=CaptionEvidenceError(
            "YTDLP_CAPTION_UNAVAILABLE",
            "공개 자막이 없는 영상입니다.",
        )
    )
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=provider,
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "YTDLP_CAPTION_UNAVAILABLE"
    assert not any(
        call[:2] in (
            ["source", "get"],
            ["source", "add"],
            ["notebook", "query"],
        )
        for call in runner.calls
    )
    assert not (tmp_path / "reviews" / f"{JOB_ID}.json").exists()
    assert not (tmp_path / "transcripts" / f"{JOB_ID}.txt").exists()


def test_unmatched_notebooklm_evidence_is_action_required_without_quote_persistence(
    tmp_path: Path,
) -> None:
    bad = valid_draft()
    bad["claims"][0]["evidence_quote"] = (
        "자막에 실제로 존재하지 않는 충분히 긴 모델 환각 문구입니다"
    )
    runner = FakeRunner(draft=bad)
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["review_required"] == [JOB_ID]
    persisted = json.dumps(queue.job, ensure_ascii=False)
    assert "모델 환각 문구" not in persisted
    assert (tmp_path / "reviews" / f"{JOB_ID}.json").exists()


def test_malformed_notebooklm_json_gets_one_strict_retry(
    tmp_path: Path,
) -> None:
    class RetryRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                if self.query_calls == 1:
                    self.calls.append(args)
                    return json.dumps({"answer": "JSON 형식이 아닌 응답"})
            return super().__call__(args, timeout)

    runner = RetryRunner()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=FakeQueue(),
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert runner.query_calls == 3


def test_format_retry_prompt_over_cap_fails_before_second_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    first_prompt = "x" * 32_700
    assert len(first_prompt.encode("utf-8")) <= 32 * 1024
    with pytest.raises(KnowledgeError, match="retry prompt exceeds"):
        knowledge_module.build_format_retry_prompt(first_prompt)

    class BoundaryRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                self.calls.append(args)
                return json.dumps({"answer": "not valid json"})
            return super().__call__(args, timeout)

    monkeypatch.setattr(knowledge_module, "build_candidate_query_prompt", lambda job, candidates: first_prompt)
    runner = BoundaryRunner()
    queue = FakeQueue()
    report = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    ).process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "NLM_DRAFT_CONTRACT_INVALID"
    assert runner.query_calls == 1


def test_ungrounded_fact_fails_without_second_query(
    tmp_path: Path,
) -> None:
    bad = valid_draft()
    bad["claims"][0]["evidence_quote"] = (
        "자막과 원문에 존재하지 않는 충분히 긴 모델 환각 문구입니다"
    )

    class FailureRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__(draft=bad)
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                if self.query_calls == 2:
                    self.draft = valid_draft()
            return super().__call__(args, timeout)

    runner = FailureRunner()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=FakeQueue(),
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert runner.query_calls == 2


def test_long_ungrounded_coverage_fails_without_second_query(tmp_path: Path) -> None:
    """P0 must not ask a second model query after a long-video quote failure."""
    verified = {
        "start": "start verified evidence appears in both source and captions exactly once",
        "middle": "middle verified evidence appears in both source and captions exactly once",
        "end": "end verified evidence appears in both source and captions exactly once",
    }
    lines = ["WEBVTT", ""]
    for index in range(1139):
        start = index * 5
        end = start + 4
        hours, remainder = divmod(start, 3600)
        minutes, seconds = divmod(remainder, 60)
        end_hours, end_remainder = divmod(end, 3600)
        end_minutes, end_seconds = divmod(end_remainder, 60)
        text = {
            0: verified["start"],
            380: verified["middle"],
            760: verified["end"],
        }.get(index, f"background caption segment {index} with unrelated context")
        lines.extend(
            (
                f"{hours:02d}:{minutes:02d}:{seconds:02d}.000 --> {end_hours:02d}:{end_minutes:02d}:{end_seconds:02d}.000",
                text,
            )
        )
    evidence = CaptionEvidence.from_vtt("\n".join(lines))
    source = ("background source context " * 2400) + " ".join(verified.values())
    assert len(source) >= 52_076
    assert len(evidence.cues) == 1139

    bad = valid_draft()
    bad["claims"][0]["evidence_quote"] = verified["start"]
    for item in bad["coverage"].values():
        item["evidence_quote"] = "model invented quote that is absent from both evidence stores"

    class LongFailureRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__(draft=bad)
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "get"]:
                self.calls.append(args)
                return source
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
            return super().__call__(args, timeout)

    runner = LongFailureRunner()
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(evidence),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert runner.query_calls == 2


def test_long_caption_initial_grounding_succeeds_without_second_query(tmp_path: Path) -> None:
    quotes = {
        "start": "start grounded evidence contains eight stable source words exactly now",
        "middle": "middle grounded evidence contains eight stable source words exactly now",
        "end": "end grounded evidence contains eight stable source words exactly now",
    }
    lines = ["WEBVTT", ""]
    for index in range(1139):
        start = index * 5
        end = start + 4
        hours, remainder = divmod(start, 3600)
        minutes, seconds = divmod(remainder, 60)
        end_hours, end_remainder = divmod(end, 3600)
        end_minutes, end_seconds = divmod(end_remainder, 60)
        text = {0: quotes["start"], 380: quotes["middle"], 760: quotes["end"]}.get(
            index, f"background caption segment {index} with unrelated context"
        )
        lines.extend((
            f"{hours:02d}:{minutes:02d}:{seconds:02d}.000 --> {end_hours:02d}:{end_minutes:02d}:{end_seconds:02d}.000",
            text,
        ))
    evidence = CaptionEvidence.from_vtt("\n".join(lines))
    source = ("background source context " * 2400) + " ".join(quotes.values())
    draft = valid_draft()
    draft["claims"][0]["evidence_quote"] = quotes["start"]
    for part, quote in quotes.items():
        draft["coverage"][part]["evidence_quote"] = quote

    class LongGroundedRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__(draft=draft)
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "get"]:
                self.calls.append(args)
                return source
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
            return super().__call__(args, timeout)

    runner = LongGroundedRunner()
    service = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=FakeQueue(), review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(evidence),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert runner.query_calls == 2


def test_large_source_and_many_cues_use_bounded_timestamp_windows() -> None:
    quotes = {
        "start": "start verified quote has eight stable source words now here",
        "middle": "middle verified quote has eight stable source words now here",
        "end": "end verified quote has eight stable source words now here",
    }
    cue_count = 6_000
    cues = tuple(
        CaptionCue(
            float(index),
            float(index + 1),
            {0: quotes["start"], 2_000: quotes["middle"], 4_000: quotes["end"]}.get(
                index, "bounded caption context remains small"
            ),
        )
        for index in range(cue_count)
    )
    evidence = CaptionEvidence(cues, "b" * 64, float(cue_count))
    filler = "source padding remains bounded and deterministic "
    source = (
        quotes["start"] + " "
        + filler * 3_800
        + quotes["middle"] + " "
        + filler * 3_800
        + quotes["end"] + " "
        + filler * 3_800
    )
    assert len(source) > 128 * 1024
    draft = valid_draft()
    draft["claims"][0]["evidence_quote"] = quotes["start"]
    for part, quote in quotes.items():
        draft["coverage"][part]["evidence_quote"] = quote

    grounded = ground_draft_with_caption_evidence(draft, evidence, source)

    assert grounded["coverage"]["start"]["citation"] == "[00:00]"
    assert grounded["coverage"]["middle"]["citation"] == "[33:20]"
    assert grounded["coverage"]["end"]["citation"] == "[01:06:40]"


def test_caption_normalization_allows_only_formatting_differences() -> None:
    quote = "Cafe ABC confirms eight stable words for source evidence today"
    evidence = CaptionEvidence(
        (CaptionCue(1.0, 5.0, "Cafe\u200b \uff21\uff22\uff23 confirms eight stable words for source evidence today"),),
        "c" * 64,
        5.0,
    )

    assert evidence.locate(quote) == 1.0
    with pytest.raises(KnowledgeError):
        evidence.locate("Cafe ABC confirms eight stable terms for source evidence today")


def test_caption_input_limits_fail_before_grounding_or_second_query(tmp_path: Path) -> None:
    # Construct directly to avoid spending parser time on an intentionally hostile VTT.
    huge_evidence = CaptionEvidence(
        cues=tuple(
            CaptionCue(float(index), float(index + 1), "bounded cue text")
            for index in range(100_000)
        ),
        evidence_hash="a" * 64,
        duration_seconds=100_000.0,
    )
    bad = valid_draft()
    for item in bad["coverage"].values():
        item["evidence_quote"] = "model invented quote that is absent from both evidence stores"

    class LimitRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__(draft=bad)
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
            return super().__call__(args, timeout)

    runner = LimitRunner()
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(huge_evidence),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "NLM_EVIDENCE_NOT_GROUNDED"
    assert runner.query_calls == 0


@pytest.mark.parametrize("field", ["citation", "promotion"])
def test_oversized_ignored_model_fields_fail_before_grounding(tmp_path: Path, field: str) -> None:
    bad = valid_draft()
    for item in bad["coverage"].values():
        item["evidence_quote"] = "model invented quote that is absent from both evidence stores"
    if field == "citation":
        bad["claims"][0]["citation"] = "x" * 257
    else:
        bad["promotion_candidates"]["concepts"] = ["x" * 8_193]

    class OversizedRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__(draft=bad)
            self.query_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["notebook", "query"]:
                self.query_calls += 1
                if "EVIDENCE_CANDIDATES=" in args[3]:
                    self.calls.append(args)
                    return json.dumps({"answer": json.dumps(self.draft, ensure_ascii=False)})
            return super().__call__(args, timeout)

    runner = OversizedRunner()
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert runner.query_calls == 1


def test_quality_failure_stays_action_required(tmp_path: Path) -> None:
    bad = valid_draft()
    bad["key_points"] = ["핵심 하나", "핵심 둘"]
    runner = FakeRunner(draft=bad)
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(runner),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={"KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1"},
    )

    report = service.process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "QUALITY_GATE_FAILED"
    assert not (tmp_path / "reviews" / f"{JOB_ID}.json").exists()


def test_process_stops_at_observed_eighty_percent_capacity(tmp_path: Path) -> None:
    class CapacityRunner(FakeRunner):
        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                return json.dumps(
                    {
                        "sources": [
                            {
                                "id": "other-source",
                                "title": "다른 영상",
                                "url": "https://www.youtube.com/watch?v=other_VIDEO1",
                            }
                        ]
                    }
                )
            return super().__call__(args, timeout)

    registry = NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"])
    registry.append(
        SourceInfo(
            "nb1",
            "YT · 미분류 · Inbox",
            "other-source",
            "다른 영상",
            "https://www.youtube.com/watch?v=other_VIDEO1",
            datetime.now(timezone.utc).isoformat(),
        )
    )
    queue = FakeQueue()
    service = KnowledgeService(
        NotebookLmClient(CapacityRunner()),
        registry,
        queue=queue,
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_NOTEBOOK_ALLOWLIST": '["nb1"]',
            "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT": "1",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
    )

    report = service.process(limit=1)

    assert report["action_required"] == [JOB_ID]
    assert queue.job["failure_code"] == "NOTEBOOKLM_LIMIT_REACHED"


def test_evaluate_draft_requires_timestamp_for_each_fact() -> None:
    draft = ground_draft_with_caption_evidence(
        valid_draft(),
        caption_evidence(),
    )
    draft["claims"].append({"type": "fact", "statement": "근거 없음"})
    quality = evaluate_draft(draft, transcript_evidence=True)
    assert quality["passed"] is False
    assert any("타임스탬프" in item for item in quality["hard_failures"])


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Authentication expired. Run nlm login", "NOTEBOOKLM_AUTH_REQUIRED"),
        ("No captions or transcript are available", "NOTEBOOKLM_CAPTION_UNAVAILABLE"),
        ("Private video", "NOTEBOOKLM_VIDEO_UNAVAILABLE"),
        ("Notebook source limit reached", "NOTEBOOKLM_LIMIT_REACHED"),
        ("Source is still processing", "NOTEBOOKLM_SOURCE_NOT_READY"),
        ("unexpected response", "NLM_PROCESSING_FAILED"),
    ],
)
def test_processing_failures_map_to_action_codes(message: str, expected: str) -> None:
    assert processing_failure_code(KnowledgeError(message)) == expected


def test_notebooklm_cli_failure_classifies_then_discards_stdout_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailedCommand:
        returncode = 1
        stdout = "Authentication expired. Run nlm login sensitive source excerpt"
        stderr = ""

    monkeypatch.setattr("core.knowledge.subprocess.run", lambda *args, **kwargs: FailedCommand())

    with pytest.raises(KnowledgeError, match="NotebookLM 로그인이 필요") as raised:
        run_notebooklm_command(["notebook", "list", "--json"])

    assert processing_failure_code(raised.value) == "NOTEBOOKLM_AUTH_REQUIRED"
    assert "sensitive source excerpt" not in str(raised.value)


def test_notebooklm_cli_uses_current_domain_compatible_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    monkeypatch.setenv("FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak-either")
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "canary")
    monkeypatch.setenv("UV_INDEX_PRIVATE_PASSWORD", "must-not-leak-uv-index")
    monkeypatch.setenv("NLM_INTERNAL_TOKEN", "must-not-leak-nlm")

    class SuccessfulCommand:
        returncode = 0
        stdout = '{"notebooks": []}'
        stderr = ""

    def fake_run(command: list[str], **kwargs: Any) -> SuccessfulCommand:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SuccessfulCommand()

    monkeypatch.setattr("core.knowledge.subprocess.run", fake_run)

    assert run_notebooklm_command(["notebook", "list", "--json"]) == '{"notebooks": []}'
    assert observed["command"] == [
        "uvx",
        "--from",
        "notebooklm-mcp-cli==0.9.4",
        "nlm",
        "notebook",
        "list",
        "--json",
    ]
    assert observed["kwargs"]["shell"] is False
    assert "NOTEBOOKLM_PROFILE" not in observed["kwargs"]["env"]
    assert "FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY" not in observed["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in observed["kwargs"]["env"]
    assert "UV_INDEX_PRIVATE_PASSWORD" not in observed["kwargs"]["env"]
    assert "NLM_INTERNAL_TOKEN" not in observed["kwargs"]["env"]


def test_notebooklm_source_get_stdout_is_read_with_a_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class Pipe:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self, size: int) -> bytes:
            observed.setdefault("read_sizes", []).append(size)
            chunk, self.payload = self.payload[:size], self.payload[size:]
            return chunk

    class Process:
        def __init__(self) -> None:
            self.stdout = Pipe(b'{"content":"normal source"}')
            self.stdin = type("Gate", (), {"write": lambda self, value: len(value), "flush": lambda self: None, "close": lambda self: None})()

        def wait(self, timeout: int) -> int:
            observed["timeout"] = timeout
            return 0

    monkeypatch.setattr("core.knowledge.subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(knowledge_module, "_assign_windows_kill_job", lambda process: 1)
    monkeypatch.setattr(knowledge_module, "_close_windows_job", lambda job: None)

    assert run_notebooklm_command(["source", "get", "source-1", "--json"]) == '{"content":"normal source"}'
    assert max(observed["read_sizes"]) <= 64 * 1024
    assert 119 <= observed["timeout"] <= 120


def test_notebooklm_source_get_rejects_oversized_stdout_before_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {"killed": False}

    class Pipe:
        def __init__(self) -> None:
            self.reads = 0

        def read(self, size: int) -> bytes:
            self.reads += 1
            return b"x" * size if self.reads <= 100 else b""

    class Process:
        def __init__(self) -> None:
            self.stdout = Pipe()
            self.stdin = type("Gate", (), {"write": lambda self, value: len(value), "flush": lambda self: None, "close": lambda self: None})()

        def kill(self) -> None:
            observed["killed"] = True

        def wait(self, timeout: int) -> int:
            return 0

    monkeypatch.setattr("core.knowledge.subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(knowledge_module, "_assign_windows_kill_job", lambda process: 1)
    monkeypatch.setattr(knowledge_module, "_close_windows_job", lambda job: None)

    with pytest.raises(KnowledgeError, match="output exceeds"):
        run_notebooklm_command(["source", "get", "source-1", "--json"])
    assert observed["killed"] is True


@pytest.mark.parametrize(
    "child_code",
    [
        "import time; time.sleep(10)",
        "import os, sys, time; os.close(sys.stdout.fileno()); time.sleep(10)",
    ],
)
def test_notebooklm_source_get_deadline_kills_and_reaps_open_or_eof_child(
    monkeypatch: pytest.MonkeyPatch, child_code: str,
) -> None:
    original_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def tracked_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("core.knowledge.subprocess.Popen", tracked_popen)
    started = time.monotonic()
    with pytest.raises(KnowledgeError, match="timed out"):
        knowledge_module._run_capped_source_get_command(
            [sys.executable, "-c", child_code], 0.2,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert children[0].poll() is not None


def test_notebooklm_source_get_exact_cap_and_overflow_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def tracked_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("core.knowledge.subprocess.Popen", tracked_popen)
    exact = knowledge_module._run_capped_source_get_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * (4 * 1024 * 1024))"], 5,
    )
    assert len(exact.encode("utf-8")) == 4 * 1024 * 1024

    with pytest.raises(KnowledgeError, match="output exceeds"):
        knowledge_module._run_capped_source_get_command(
            [sys.executable, "-c", "import sys, time; sys.stdout.buffer.write(b'x' * (16 * 1024 * 1024)); sys.stdout.flush(); time.sleep(10)"], 5,
        )
    assert children[-1].poll() is not None
    assert not any(
        thread.name == "knowledge-source-get-reader" and thread.is_alive()
        for thread in threading.enumerate()
    )


@pytest.mark.skipif(os.name == "nt", reason="Unix process-group assertion")
def test_notebooklm_source_get_timeout_kills_unix_grandchild(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(10)"
    )

    with pytest.raises(KnowledgeError, match="timed out"):
        knowledge_module._run_capped_source_get_command(
            [sys.executable, "-c", code, str(pid_path)], 0.2,
        )

    grandchild_pid = int(pid_path.read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("source_get grandchild remained alive after process-group cleanup")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object assertion")
def test_notebooklm_source_get_timeout_kills_windows_parent_exit_grandchild(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    code = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )
    started = time.monotonic()
    with pytest.raises(KnowledgeError, match="timed out"):
        knowledge_module._run_capped_source_get_command(
            [sys.executable, "-c", code, str(pid_path)], 0.2,
        )
    assert time.monotonic() - started < 1
    grandchild_pid = int(pid_path.read_text(encoding="utf-8"))
    for _ in range(10):
        try:
            os.kill(grandchild_pid, 0)
        except (OSError, SystemError):
            break
        time.sleep(0.05)
    else:
        pytest.fail("source_get grandchild remained alive after Job cleanup")


@pytest.mark.skipif(os.name != "nt", reason="Windows launch-gate assertion")
def test_notebooklm_source_get_job_assignment_failure_never_runs_gated_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    marker = tmp_path / "should-not-exist"
    monkeypatch.setattr(knowledge_module, "_assign_windows_kill_job", lambda process: None)
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"

    with pytest.raises(KnowledgeError, match="ownership failed"):
        knowledge_module._run_capped_source_get_command(
            [sys.executable, "-c", code], 1,
        )

    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows launch-gate assertion")
def test_notebooklm_source_get_gate_write_failure_never_runs_gated_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    marker = tmp_path / "should-not-exist"
    original_popen = subprocess.Popen

    class BrokenGate:
        def write(self, value: bytes) -> int:
            raise OSError("gate unavailable")

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class ProcessProxy:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            self.process = process
            self.stdout = process.stdout
            self.stdin = BrokenGate()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.process, name)

    monkeypatch.setattr(
        "core.knowledge.subprocess.Popen",
        lambda *args, **kwargs: ProcessProxy(original_popen(*args, **kwargs)),
    )
    monkeypatch.setattr(
        "core.knowledge.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
    )
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"

    with pytest.raises(KnowledgeError, match="launch gate failed"):
        knowledge_module._run_capped_source_get_command(
            [sys.executable, "-c", code], 1,
        )

    assert not marker.exists()


def test_queue_error_never_exposes_service_key() -> None:
    secret = "secret_service_role_key_1234567890"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == secret
        return httpx.Response(500, text=f"upstream rejected {secret}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    queue = FocusFeedQueue(
        "https://example.supabase.co",
        secret,
        OWNER_USER_ID,
        client=client,
    )
    with pytest.raises(KnowledgeError) as captured:
        queue.get(JOB_ID)
    assert secret not in str(captured.value)
    assert "[redacted]" in str(captured.value)


def test_worker_complete_allows_only_nonterminal_queue_states() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[make_job()])

    queue = FocusFeedQueue(
        "https://example.supabase.co",
        "service-role-key",
        OWNER_USER_ID,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    job = make_job()

    for status in ("review_required", "action_required"):
        queue.complete(job, status)

    assert [request.url.path for request in requests] == [
        "/rest/v1/rpc/complete_knowledge_job",
        "/rest/v1/rpc/complete_knowledge_job",
    ]

    requests.clear()
    for status in ("completed", "failed"):
        with pytest.raises(KnowledgeError, match="완료 상태"):
            queue.complete(job, status)

    assert requests == []


def test_service_role_queue_is_scoped_to_configured_owner() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path.endswith("/rpc/claim_knowledge_jobs"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[make_job()])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    queue = FocusFeedQueue(
        "https://example.supabase.co",
        "service-role-key",
        OWNER_USER_ID,
        client=client,
    )

    queue.get(JOB_ID)
    queue.reviews()
    queue.claim("knowledge-worker:test", 1)
    queue.retry(JOB_ID)
    queue.invalidate_review(JOB_ID)
    job = make_job()
    queue.checkpoint(job)
    queue.complete(job, "review_required")
    queue.begin_approval(job, "a" * 64)
    queue.mark_completed(
        {**job, "approval_token": "33333333-3333-4333-8333-333333333333"},
        {},
    )
    queue.defer(job)
    queue.reject(job)

    table_requests = [request for request in observed if request.url.path.endswith("/knowledge_jobs")]
    rpc_requests = [request for request in observed if "/rpc/" in request.url.path]
    assert table_requests
    assert rpc_requests
    assert all(request.url.params["user_id"] == f"eq.{OWNER_USER_ID}" for request in table_requests)
    assert all(json.loads(request.content)["p_user_id"] == OWNER_USER_ID for request in rpc_requests)


def test_retry_contract_preserves_notebook_identity_and_attempt_ceiling(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "action_required",
            "attempt_count": 2,
            "failure_code": "NLM_EVIDENCE_NOT_GROUNDED",
            "failure_message": "근거 확인 필요",
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "source-hash",
            "transcript_hash": "caption-hash",
        }
    )
    queue = FakeQueue(job)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        env={},
    )

    result = service.retry(JOB_ID)

    assert result["status"] == "queued"
    assert result["attempt_count"] == 2
    assert result["notebook_id"] == "nb1"
    assert result["notebook_source_id"] == "source-nb1"
    assert queue.job["source_hash"] == "source-hash"
    assert queue.job["transcript_hash"] == "caption-hash"

    queue.job.update({"status": "action_required", "attempt_count": 3})
    with pytest.raises(KnowledgeError, match="not eligible"):
        service.retry(JOB_ID)


@pytest.mark.parametrize(
    "metadata",
    [
        {"_canary_no_retry": True},
        {"_legacy_review_recovery_v1": True},
        {"_review_staging_conflict_recovery_v1": {"recovered_at": "now"}},
    ],
)
def test_retry_refuses_clean_and_recovery_jobs(
    tmp_path: Path,
    metadata: dict[str, Any],
) -> None:
    job = make_job()
    job.update(
        {
            "status": "action_required",
            "attempt_count": 1,
            "failure_code": "QUALITY_GATE_FAILED",
            "metadata": metadata,
        }
    )
    queue = FakeQueue(job)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        env={},
    )

    with pytest.raises(KnowledgeError, match="permanently excluded"):
        service.retry(JOB_ID)

    assert queue.job["status"] == "action_required"


def test_canary_inspect_reconstructs_eligible_jobs_from_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "canary.json"
    manifest.write_text(json.dumps({"items": [{"url": VIDEO_URL}]}), encoding="utf-8")
    run_id, _ = knowledge_module.canary_manifest_run_id(manifest)
    assert run_id == knowledge_module.sha256_text(
        json.dumps([VIDEO_URL], ensure_ascii=False, separators=(",", ":"))
    )
    job = make_job()
    job.update(
        {
            "status": "queued",
            "attempt_count": 0,
            "capture_ready": True,
            "lease_token": None,
            "metadata": {
                "_canary_run_id": run_id,
                "_canary_hold": True,
                "_canary_no_retry": True,
            },
        }
    )
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=FakeQueue(job),
        env={},
    )

    report = service.canary_inspect(manifest)

    assert report["run_id"] == run_id
    assert report["expected"] == 1
    assert report["found"] == 1
    assert report["eligible"] == 1
    assert report["items"][0]["job_id"] == JOB_ID


def test_canary_manifest_run_id_matches_focus_feed_normalized_url_contract(tmp_path: Path) -> None:
    manifest = tmp_path / "canary-two.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {"url": "https://youtu.be/abc_DEF-123"},
                    {"url": "https://www.youtube.com/watch?v=xyz_ABC-789"},
                ]
            }
        ),
        encoding="utf-8",
    )

    run_id, video_ids = knowledge_module.canary_manifest_run_id(manifest)

    assert run_id == "bceb9a4c4cd505e5ac97b20272024dac07de4d97c88ccaf15c7219dce3ed6adb"
    assert video_ids == ("abc_DEF-123", "xyz_ABC-789")


def test_cli_exposes_explicit_single_job_retry_command() -> None:
    args = build_parser().parse_args(["retry", JOB_ID])
    assert args.command == "retry"
    assert args.job_id == JOB_ID


def test_cli_exposes_exact_process_doctor_and_canary_inspect() -> None:
    exact = build_parser().parse_args(
        ["process", "--job-id", JOB_ID, "--expected-git-sha", "a" * 40]
    )
    assert exact.job_id == JOB_ID
    assert exact.limit is None
    assert exact.expected_git_sha == "a" * 40
    doctor = build_parser().parse_args(["doctor", "--job-id", JOB_ID])
    assert doctor.job_id == JOB_ID
    canary = build_parser().parse_args(["canary", "inspect", "--manifest", "canary.json"])
    assert canary.canary_command == "inspect"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["process", "--job-id", JOB_ID, "--limit", "1", "--expected-git-sha", "a" * 40],
            "cannot be used together",
        ),
        (["process", "--expected-git-sha", "a" * 40], "requires --job-id"),
    ],
)
def test_cli_rejects_ambiguous_exact_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    message: str,
) -> None:
    monkeypatch.setattr(
        knowledge_cli.KnowledgeService,
        "from_env",
        classmethod(lambda cls, require_queue=False: object()),
    )

    with pytest.raises(KnowledgeError, match=message):
        knowledge_cli.run(build_parser().parse_args(argv))


def test_invalid_legacy_review_moves_to_action_required_without_losing_source_identity(tmp_path: Path) -> None:
    job = make_job()
    draft = valid_draft()
    draft["claims"][0].pop("citation")
    job.update(
        {
            "status": "review_required",
            "attempt_count": 1,
            "result": {"draft": draft},
            "quality_report": approval_quality(),
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
        }
    )
    queue = FakeQueue(job)
    review_store = ReviewStore(tmp_path / "runtime" / "reviews")
    original_review = {"job_id": JOB_ID, "draft": {"summary": "old review"}}
    review_store.write(JOB_ID, original_review)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=review_store,
        env={},
    )

    result = service.invalidate_review(JOB_ID)

    assert result["status"] == "action_required"
    assert result["failure_code"] == "PUBLIC_CAPTION_TIMESTAMPS_REQUIRED"
    assert queue.job["notebook_id"] == "nb1"
    assert queue.job["notebook_source_id"] == "source-nb1"
    assert queue.job["source_hash"] == "a" * 64
    assert queue.job["transcript_hash"] == "b" * 64
    assert queue.job["result"]["draft"]["claims"]
    assert result["review_archived"] is True
    assert not (review_store.root / f"{JOB_ID}.json").exists()
    archives = list((review_store.root.parent / "invalidated-reviews").glob(f"{JOB_ID}-*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8")) == original_review

    replacement = {"job_id": JOB_ID, "draft": {"summary": "new review"}}
    assert json.loads(review_store.write(JOB_ID, replacement).read_text(encoding="utf-8")) == replacement


def test_review_invalidation_rejects_missing_or_changed_source_identity(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "attempt_count": 1,
            "result": {"draft": valid_draft()},
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
        }
    )
    queue = FakeQueue(job)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        env={},
    )

    queue.job["source_hash"] = None
    with pytest.raises(KnowledgeError, match="source/hash"):
        service.invalidate_review(JOB_ID)

    queue.job["source_hash"] = "a" * 64
    original_invalidate = queue.invalidate_review

    def corrupting_invalidate(job_id: str) -> dict[str, Any]:
        invalidated = original_invalidate(job_id)
        invalidated["notebook_source_id"] = "changed-source"
        return invalidated

    queue.invalidate_review = corrupting_invalidate  # type: ignore[method-assign]
    with pytest.raises(KnowledgeError, match="식별자가 변경"):
        service.invalidate_review(JOB_ID)


def test_review_invalidation_rejects_valid_timestamps_wrong_status_and_attempt_ceiling(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "attempt_count": 1,
            "result": {"draft": grounded_draft()},
            "quality_report": approval_quality(),
        }
    )
    queue = FakeQueue(job)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        env={},
    )

    with pytest.raises(KnowledgeError, match="타임스탬프가 완전"):
        service.invalidate_review(JOB_ID)
    queue.job["status"] = "completed"
    with pytest.raises(KnowledgeError, match="검토 대기"):
        service.invalidate_review(JOB_ID)
    queue.job.update({"status": "review_required", "attempt_count": 2, "metadata": {"_legacy_review_recovery_v1": True}})
    with pytest.raises(KnowledgeError, match="1회 복구 한도"):
        service.invalidate_review(JOB_ID)


def test_attempt_three_legacy_review_gets_exactly_one_bounded_recovery(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "attempt_count": 3,
            "metadata": {},
            "result": {"draft": valid_draft()},
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
        }
    )
    queue = FakeQueue(job)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        env={},
    )

    result = service.invalidate_review(JOB_ID)

    assert result["status"] == "action_required"
    assert result["attempt_count"] == 2
    assert queue.job["metadata"]["_legacy_review_recovery_v1"] is True


def test_cli_exposes_explicit_legacy_review_invalidation_command() -> None:
    args = build_parser().parse_args(["invalidate-review", JOB_ID])
    assert args.command == "invalidate-review"
    assert args.job_id == JOB_ID


def test_queue_env_fails_closed_without_owner_user_id() -> None:
    with pytest.raises(KnowledgeError, match="FOCUS_FEED_OWNER_USER_ID"):
        FocusFeedQueue.from_env(
            {
                "FOCUS_FEED_SUPABASE_URL": "https://example.supabase.co",
                "FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
            }
        )


def test_approval_is_write_once_and_human_note_only_enters_summary(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "lease_token": None,
            "quality_score": 95,
            "quality_report": approval_quality(),
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
            "result": {"draft": grounded_draft()},
        }
    )
    queue = FakeQueue(job)
    brain_root = tmp_path / "brain"
    (brain_root / "memory").mkdir(parents=True)
    writer = BrainWriter(brain_root)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=ReviewStore(tmp_path / "runtime" / "reviews"),
        brain_writer_factory=lambda: writer,
        env={},
    )

    first = service.approve(JOB_ID, "내 적용 메모")
    second = service.approve(JOB_ID, "다른 메모는 재승인에 사용되지 않음")

    resource = tmp_path / "brain" / first["resource_path"]
    insight = tmp_path / "brain" / first["insight_path"]
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert "reviewed_by: yohan" in resource.read_text(encoding="utf-8")
    assert f"job_id: {JOB_ID}" in insight.read_text(encoding="utf-8")
    assert "내 적용 메모" not in resource.read_text(encoding="utf-8")
    assert "내 적용 메모" in insight.read_text(encoding="utf-8")
    assert "내 적용 메모" not in json.dumps(queue.job, ensure_ascii=False)


def test_brain_writer_uses_explicit_empty_note_without_trailing_whitespace(tmp_path: Path) -> None:
    job = make_job()
    job.update({"id": JOB_ID, "result": {"draft": grounded_draft()}})
    brain_root = tmp_path / "brain"
    (brain_root / "memory").mkdir(parents=True)

    written = BrainWriter(brain_root).write(job, "", "2026-08-05T00:00:00Z")
    insight = brain_root / written["insight_path"]
    content = insight.read_text(encoding="utf-8")

    assert "## 내 생각\n- 없음\n" in content
    assert all(not line.endswith((" ", "\t")) for line in content.splitlines())


def test_approval_recovers_resource_summary_pair_after_mid_write_crash(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "lease_token": None,
            "quality_score": 95,
            "quality_report": approval_quality(),
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
            "result": {"draft": grounded_draft()},
        }
    )
    queue = FakeQueue(job)
    brain_root = tmp_path / "brain"
    (brain_root / "memory").mkdir(parents=True)

    class CrashOnceWriter(BrainWriter):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.calls = 0
            self.crashed = False

        def _write_once(self, path: Path, content: str) -> bool:
            self.calls += 1
            if self.calls == 2 and not self.crashed:
                self.crashed = True
                raise KnowledgeError("injected crash between approval files")
            return BrainWriter._write_once(path, content)

    writer = CrashOnceWriter(brain_root)
    store = ReviewStore(tmp_path / "runtime" / "reviews")
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        review_store=store,
        brain_writer_factory=lambda: writer,
        env={},
    )

    with pytest.raises(KnowledgeError, match="injected crash"):
        service.approve(JOB_ID, "복구할 승인 메모")
    assert queue.job["status"] == "approving"
    assert (brain_root / "memory" / "ingest" / "url" / f"knowledge-{JOB_ID}.md").exists()
    assert not (brain_root / "memory" / "ingest" / "insights" / f"knowledge-{JOB_ID}.md").exists()

    recovered = service.approve(JOB_ID)

    assert recovered["idempotent"] is False
    assert queue.job["status"] == "completed"
    insight = brain_root / "memory" / "ingest" / "insights" / f"knowledge-{JOB_ID}.md"
    assert "복구할 승인 메모" in insight.read_text(encoding="utf-8")
    assert (tmp_path / "runtime" / "approval-intents" / f"{JOB_ID}.json").exists()


def test_approval_below_quality_gate_does_not_write(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "lease_token": None,
            "quality_score": 84,
            "result": {"draft": grounded_draft()},
        }
    )
    brain_root = tmp_path / "brain"
    (brain_root / "memory").mkdir(parents=True)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=FakeQueue(job),
        brain_writer_factory=lambda: BrainWriter(brain_root),
        env={},
    )
    with pytest.raises(KnowledgeError, match="승인 기준"):
        service.approve(JOB_ID)
    assert not (brain_root / "memory" / "ingest").exists()


def test_approve_rejects_incomplete_contract_without_creating_intent_or_writing(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "lease_token": None,
            "quality_score": 95,
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
            "result": {"draft": grounded_draft()},
            # The score is deliberately high but the reviewed evidence contract is absent.
            "quality_report": {"score": 95, "passed": True},
        }
    )
    queue = FakeQueue(job)
    brain_root = tmp_path / "brain"
    (brain_root / "memory").mkdir(parents=True)
    store = ReviewStore(tmp_path / "runtime" / "reviews")
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=store, brain_writer_factory=lambda: BrainWriter(brain_root), env={},
    )

    with pytest.raises(KnowledgeError, match="근거 방식"):
        service.approve(JOB_ID)
    assert queue.job["status"] == "review_required"
    assert not (tmp_path / "runtime" / "approval-intents").exists()
    assert not (brain_root / "memory" / "ingest").exists()


def test_t3_approval_requires_independent_evaluator_before_write(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required", "lease_token": None, "tier": "T3",
            "quality_score": 95,
            "quality_report": approval_quality(),
            "notebook_id": "nb1", "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64, "transcript_hash": "b" * 64,
            "result": {"draft": grounded_draft()},
        }
    )
    queue = FakeQueue(job)
    brain_root = tmp_path / "brain"
    (brain_root / "memory").mkdir(parents=True)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()), NotebookRegistry(tmp_path / "registry.json"),
        queue=queue, review_store=ReviewStore(tmp_path / "runtime" / "reviews"),
        brain_writer_factory=lambda: BrainWriter(brain_root), env={},
    )

    with pytest.raises(KnowledgeError, match="second_evaluator"):
        service.approve(JOB_ID)
    assert queue.job["status"] == "review_required"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression fixture")
def test_brain_writer_rejects_windows_parent_junction(tmp_path: Path) -> None:
    brain_root = tmp_path / "brain"
    (brain_root / "memory").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = brain_root / "memory" / "ingest"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True, check=False, shell=False,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable in this Windows test environment")
    job = make_job()
    job.update({"id": JOB_ID, "result": {"draft": grounded_draft()}})

    with pytest.raises(KnowledgeError, match="안전하지 않습니다"):
        BrainWriter(brain_root).write(job, "", "2026-08-05T00:00:00Z")
    assert not any(outside.iterdir())


def test_reviews_expose_control_tower_contract_without_transcript(tmp_path: Path) -> None:
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "title": "검토할 영상",
            "quality_score": 91,
            "quality_report": {**approval_quality(), "warnings": ["고유명사 확인 필요"]},
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
            "result": {"draft": grounded_draft()},
        }
    )
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=FakeQueue(job),
        env={},
    )

    result = service.reviews()

    assert result["items"][0]["jobId"] == JOB_ID
    assert result["items"][0]["title"] == "검토할 영상"
    assert result["items"][0]["relevance"] == grounded_draft()["yohan_relevance"]
    assert result["items"][0]["qualityWarnings"] == ["고유명사 확인 필요"]
    assert result["items"][0]["approvalReady"] is True
    assert result["items"][0]["approvalBlockers"] == []
    assert result["items"][0]["reprocessEligible"] is False
    assert result["items"][0]["reprocessBlockers"] == ["공개 자막 타임스탬프 누락 유형이 아닙니다."]
    assert result["items"][0]["attemptCount"] == 0
    assert "transcript" not in json.dumps(result, ensure_ascii=False).lower()


def test_reviews_fail_closed_for_used_recovery_marker_and_invalid_hash(tmp_path: Path) -> None:
    draft = valid_draft()
    draft["claims"][0].pop("citation", None)
    job = make_job()
    job.update(
        {
            "status": "review_required",
            "attempt_count": 2,
            "metadata": {"_legacy_review_recovery_v1": True},
            "notebook_id": "nb1",
            "notebook_source_id": "source-nb1",
            "source_hash": "a" * 64,
            "transcript_hash": "b" * 64,
            "result": {"draft": draft},
        }
    )
    queue = FakeQueue(job)
    service = KnowledgeService(
        NotebookLmClient(FakeRunner()),
        NotebookRegistry(tmp_path / "registry.json"),
        queue=queue,
        env={},
    )

    used_marker = service.reviews()["items"][0]
    assert used_marker["reprocessEligible"] is False
    assert "레거시 검토의 1회 복구 한도에 도달했습니다." in used_marker["reprocessBlockers"]

    queue.job["metadata"] = {}
    queue.job["source_hash"] = "not-a-sha256"
    invalid_hash = service.reviews()["items"][0]
    assert invalid_hash["reprocessEligible"] is False
    assert "보존할 source/hash 식별자가 SHA-256 형식이 아닙니다." in invalid_hash["reprocessBlockers"]


def test_knowledge_runtime_never_defaults_into_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOHAN_BRAIN_ROOT", "C:/example/yohan-brain")
    monkeypatch.delenv("KNOWLEDGE_RUNTIME_DIR", raising=False)
    resolved = resolve_knowledge_runtime_dir()
    assert "yohan-mcp" in str(resolved)
    assert "yohan-brain" not in str(resolved)


def test_knowledge_runtime_rejects_explicit_path_inside_brain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = tmp_path / "brain"
    monkeypatch.setenv("YOHAN_BRAIN_ROOT", str(brain))
    monkeypatch.delenv("MEMORY_DIR", raising=False)
    monkeypatch.setenv("KNOWLEDGE_RUNTIME_DIR", str(brain / "runtime"))

    with pytest.raises(RuntimeError, match="must stay outside"):
        resolve_knowledge_runtime_dir()


def test_knowledge_runtime_rejects_explicit_path_inside_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    memory = tmp_path / "brain" / "memory"
    monkeypatch.delenv("YOHAN_BRAIN_ROOT", raising=False)
    monkeypatch.setenv("MEMORY_DIR", str(memory))
    monkeypatch.setenv("KNOWLEDGE_RUNTIME_DIR", str(memory / "reviews"))

    with pytest.raises(RuntimeError, match="must stay outside"):
        resolve_knowledge_runtime_dir()


def _raw_notebook_source(source_id: str, metadata: list[Any]) -> list[Any]:
    return [["Notebook", [[[source_id], "raw title must not escape", metadata]]]]


def test_source_identity_parser_accepts_current_type_9_metadata() -> None:
    raw = _raw_notebook_source(
        "source-current",
        [None, None, None, None, 9, ["https://youtu.be/abcDEF_1234?utm_source=x", VIDEO_ID]],
    )

    assert extract_youtube_source_identities(raw) == {"source-current": VIDEO_URL}


def test_source_identity_parser_accepts_allowlisted_scheme_less_youtube_locator() -> None:
    raw = _raw_notebook_source(
        "source-scheme-less",
        [None, None, None, None, 9, [f"youtube.com/watch?v={VIDEO_ID}", VIDEO_ID]],
    )

    assert extract_youtube_source_identities(raw) == {"source-scheme-less": VIDEO_URL}


def test_source_identity_parser_accepts_legacy_type_9_metadata() -> None:
    raw = _raw_notebook_source(
        "source-legacy",
        [None, None, None, None, 9, None, None, ["https://www.youtube.com/watch?v=abcDEF_1234"]],
    )

    assert extract_youtube_source_identities(raw) == {"source-legacy": VIDEO_URL}


def test_source_identity_parser_rejects_malformed_and_ambiguous_metadata() -> None:
    malformed = _raw_notebook_source(
        "source-malformed",
        [None, None, None, None, 9, [VIDEO_URL, "too-short"], None, [VIDEO_URL]],
    )
    ambiguous = _raw_notebook_source(
        "source-ambiguous",
        [
            None, None, None, None, 9,
            [VIDEO_URL, VIDEO_ID],
            None,
            ["https://www.youtube.com/watch?v=zyxWVUT_987"],
        ],
    )

    assert extract_youtube_source_identities(malformed) == {}
    assert extract_youtube_source_identities(ambiguous) == {}


def test_source_identity_parser_rejects_non_youtube_rows() -> None:
    raw = _raw_notebook_source(
        "source-web",
        [None, None, None, None, 8, [VIDEO_URL, VIDEO_ID]],
    )

    assert extract_youtube_source_identities(raw) == {}


def test_source_identity_helper_discards_subprocess_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "cookie-secret-value-abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY", "must-not-leak")
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "canary")
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "must-not-leak-uv")

    class FailedCommand:
        returncode = 1
        stdout = f"raw RPC {secret} title body"
        stderr = f"Bearer {secret}"

    observed: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> FailedCommand:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FailedCommand()

    monkeypatch.setattr("core.knowledge.subprocess.run", fake_run)

    with pytest.raises(KnowledgeError) as captured:
        run_notebooklm_source_identity_helper("nb1")

    assert secret not in str(captured.value)
    assert "raw RPC" not in str(captured.value)
    assert observed["command"][:4] == ["uvx", "--with", "notebooklm-mcp-cli==0.9.4", "python"]
    assert observed["kwargs"]["shell"] is False
    assert "NOTEBOOKLM_PROFILE" not in observed["kwargs"]["env"]
    assert "FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY" not in observed["kwargs"]["env"]
    assert "UV_PUBLISH_TOKEN" not in observed["kwargs"]["env"]


def test_source_identity_script_uses_authenticated_client_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import contextlib
    import sys
    import types

    observed: list[object] = []

    class Client:
        def get_notebook(self, notebook_id: str) -> list[Any]:
            observed.append(("get_notebook", notebook_id))
            return _raw_notebook_source(
                "source-current",
                [None, None, None, None, 9, [VIDEO_URL, VIDEO_ID]],
            )

    @contextlib.contextmanager
    def get_client() -> Any:
        observed.append("enter")
        try:
            yield Client()
        finally:
            observed.append("exit")

    utils = types.ModuleType("notebooklm_tools.cli.utils")
    utils.get_client = get_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "notebooklm_tools", types.ModuleType("notebooklm_tools"))
    monkeypatch.setitem(sys.modules, "notebooklm_tools.cli", types.ModuleType("notebooklm_tools.cli"))
    monkeypatch.setitem(sys.modules, "notebooklm_tools.cli.utils", utils)

    from scripts import notebooklm_source_identity

    assert notebooklm_source_identity.main(["helper", "nb-auth"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "sources": [{"source_id": "source-current", "url": VIDEO_URL}],
    }
    assert observed == ["enter", ("get_notebook", "nb-auth"), "exit"]


def test_source_identity_script_redacts_auth_context_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys
    import types

    secret = "Bearer cookie-secret-value-abcdefghijklmnopqrstuvwxyz"

    def get_client() -> Any:
        raise RuntimeError(secret)

    utils = types.ModuleType("notebooklm_tools.cli.utils")
    utils.get_client = get_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "notebooklm_tools", types.ModuleType("notebooklm_tools"))
    monkeypatch.setitem(sys.modules, "notebooklm_tools.cli", types.ModuleType("notebooklm_tools.cli"))
    monkeypatch.setitem(sys.modules, "notebooklm_tools.cli.utils", utils)

    from scripts import notebooklm_source_identity

    assert notebooklm_source_identity.main(["helper", "nb-auth"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert secret not in captured.err


def test_manual_preadded_youtube_source_reuses_identity_helper_match(tmp_path: Path) -> None:
    class NullUrlRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0

        def __call__(self, args: list[str], timeout: int) -> str:
            if args[:2] == ["source", "list"]:
                self.calls.append(args)
                return json.dumps({"sources": [{"id": "manual-source", "url": None, "type": "youtube"}]})
            if args[:2] == ["source", "add"]:
                self.add_calls += 1
            return super().__call__(args, timeout)

    runner = NullUrlRunner()
    service = KnowledgeService(
        NotebookLmClient(runner, source_identity_reader=lambda notebook_id: {"manual-source": VIDEO_URL}),
        NotebookRegistry(tmp_path / "registry.json", canonical_notebook_ids=["nb1"]),
        queue=FakeQueue(),
        review_store=ReviewStore(tmp_path / "reviews"),
        caption_provider=FakeCaptionProvider(),
        env={
            "KNOWLEDGE_NOTEBOOK_DEFAULT_ID": "nb1",
            "KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH": "1",
        },
    )

    report = service.process(limit=1)

    assert report["review_required"] == [JOB_ID]
    assert runner.add_calls == 0
    assert any(call[:2] == ["source", "get"] and call[2] == "manual-source" for call in runner.calls)

# -*- coding: utf-8 -*-
"""Focus Feed → NotebookLM → Yohan Brain knowledge workflow.

The module deliberately keeps three boundaries explicit:

* Supabase ``knowledge_jobs`` is the operational queue SoT.
* NotebookLM is an external source/query tool, never a job database.
* yohan-brain receives new write-once RESOURCE/SUMMARY files only after a
  person approves a ``review_required`` job.

No credential, transcript body, or human note is written to the registry.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from core.paths import resolve_knowledge_runtime_dir


NOTEBOOKLM_PACKAGE_SPEC = "notebooklm-mcp-cli==0.9.4"
QUALITY_THRESHOLD = 85
MAX_BATCH = 3
REGISTRY_TTL = timedelta(hours=24)
LEASE_SECONDS = 900
MIN_EVIDENCE_QUOTE_CHARS = 10
NOTEBOOKLM_SOURCE_EVIDENCE_CONTRACT = "notebooklm-source-get-v1"
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid"}
_TOKEN_PATTERN = re.compile(r"(?:Bearer\s+)?[A-Za-z0-9._-]{24,}", re.IGNORECASE)
_TIMESTAMP_PATTERN = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_VTT_TIMING_PATTERN = re.compile(
    r"^(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_CHILD_ENV_KEYS = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


def _child_process_env() -> dict[str, str]:
    """Return a minimum child environment without application credentials."""
    result = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _CHILD_ENV_KEYS
    }
    result.setdefault("PYTHONUTF8", "1")
    result.setdefault("PYTHONIOENCODING", "utf-8")
    return result


class KnowledgeError(RuntimeError):
    """Safe, user-facing workflow error."""


class LeaseLostError(KnowledgeError):
    """The claimed job is no longer owned by this worker."""


class CaptionEvidenceError(KnowledgeError):
    """A public-caption failure with a stable UI-safe code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceGroundingError(KnowledgeError):
    """NotebookLM evidence text could not be grounded in public captions."""


class DraftContractError(EvidenceGroundingError):
    """A model draft violated the strict persistence allowlist."""


class NotebookLmCommandError(KnowledgeError):
    """A NotebookLM CLI failure with a fixed, persistence-safe message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_error(error: object, limit: int = 800) -> str:
    message = str(error).replace("\x00", " ")
    message = _TOKEN_PATTERN.sub("[redacted]", message)
    return re.sub(r"\s+", " ", message).strip()[:limit]


def processing_failure_code(error: object) -> str:
    """Map unstable NotebookLM text to a small UI-safe action taxonomy."""
    if isinstance(error, CaptionEvidenceError):
        return error.code
    if isinstance(error, DraftContractError):
        return "NLM_DRAFT_CONTRACT_INVALID"
    if isinstance(error, EvidenceGroundingError):
        return "NLM_EVIDENCE_NOT_GROUNDED"
    if isinstance(error, NotebookLmCommandError):
        return error.code
    message = safe_error(error).lower()
    if "manual source identity confirmation required" in message:
        return "NOTEBOOKLM_SOURCE_IDENTITY_REQUIRED"
    if any(marker in message for marker in ("login", "cookie", "unauthorized", "forbidden", "authentication", "401", "403")):
        return "NOTEBOOKLM_AUTH_REQUIRED"
    if any(marker in message for marker in ("caption", "subtitle", "transcript", "자막")):
        return "NOTEBOOKLM_CAPTION_UNAVAILABLE"
    if any(marker in message for marker in ("private video", "video unavailable", "비공개", "사용할 수 없는 동영상")):
        return "NOTEBOOKLM_VIDEO_UNAVAILABLE"
    if any(marker in message for marker in ("quota", "capacity", "source limit", "too many sources", "한도")):
        return "NOTEBOOKLM_LIMIT_REACHED"
    if any(marker in message for marker in ("not ready", "still processing", "processing source", "pending")):
        return "NOTEBOOKLM_SOURCE_NOT_READY"
    return "NLM_PROCESSING_FAILED"


def youtube_video_id(value: str) -> str | None:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    candidate: str | None = None
    parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be" and parts:
        candidate = parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com") or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com"):
        if parsed.path == "/watch":
            candidate = dict(parse_qsl(parsed.query)).get("v")
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            candidate = parts[1]
    if candidate and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", candidate):
        return candidate
    return None


def canonical_url(value: str) -> str:
    video_id = youtube_video_id(value)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise KnowledgeError("http(s) URL만 처리할 수 있습니다.")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    kept = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((parsed.scheme.lower(), f"{host}{port}", parsed.path or "/", urlencode(sorted(kept)), ""))


def _normalize_scheme_less_youtube_locator(value: str) -> str:
    """Add HTTPS only to an explicitly allowlisted scheme-less YouTube host."""
    locator = value.strip()
    parsed = urlsplit(locator)
    if parsed.scheme or parsed.netloc:
        return locator
    host = locator.split("/", 1)[0].split("?", 1)[0].lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return f"https://{locator}"
    return locator


def source_key(url: str) -> str:
    return youtube_video_id(url) or canonical_url(url)


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class InterProcessFileLock:
    """Crash-safe advisory lock shared by every local yohan-mcp process."""

    def __init__(self, path: Path, timeout_seconds: float = 180.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "InterProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self._fd = fd
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
            os.fsync(fd)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as error:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    self._fd = None
                    raise KnowledgeError(f"local knowledge lock timeout: {self.path.name}") from error
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_once(path: Path, content: str, mode: int = 0o600) -> bool:
    """Publish only a fully-fsynced file and never replace an existing target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise KnowledgeError(f"write-once content mismatch: {path.name}")
        return False
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.chmod(mode)
        except OSError:
            pass
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise KnowledgeError(f"concurrent write-once content mismatch: {path.name}")
            return False
        try:
            path.chmod(mode)
        except OSError:
            pass
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise KnowledgeError("외부 명령의 JSON 응답 형식이 올바르지 않습니다.") from error


def _first_list(value: Any, keys: Iterable[str]) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
    for child in value.values():
        found = _first_list(child, keys)
        if found is not None:
            return found
    return None


def _first_string(value: Any, keys: Iterable[str], depth: int = 0) -> str | None:
    if depth > 8:
        return None
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = _first_string(child, keys, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_string(child, keys, depth + 1)
            if found:
                return found
    return None


CommandRunner = Callable[[list[str], int], str]
SourceIdentityReader = Callable[[str], Mapping[str, str]]


def run_notebooklm_command(args: list[str], timeout_seconds: int = 120) -> str:
    command = [
        os.getenv("KNOWLEDGE_UVX_COMMAND", "uvx").strip() or "uvx",
        "--from",
        NOTEBOOKLM_PACKAGE_SPEC,
        "nlm",
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=_child_process_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NotebookLmCommandError(
            "NLM_PROCESSING_FAILED",
            "NotebookLM 명령을 실행하지 못했습니다.",
        ) from error
    if completed.returncode != 0:
        # Rich-based CLI errors may be written to stdout. Inspect them only in
        # memory, then discard them and raise a fixed message so source excerpts
        # can never cross the Supabase failure_message boundary.
        details = "\n".join(
            part.strip()
            for part in (completed.stderr, completed.stdout)
            if part and part.strip()
        ).casefold()
        if any(marker in details for marker in ("login", "cookie", "unauthorized", "forbidden", "authentication", "401", "403")):
            code, message = "NOTEBOOKLM_AUTH_REQUIRED", "NotebookLM 로그인이 필요합니다."
        elif any(marker in details for marker in ("caption", "subtitle", "transcript", "자막")):
            code, message = "NOTEBOOKLM_CAPTION_UNAVAILABLE", "NotebookLM에서 공개 자막을 확인하지 못했습니다."
        elif any(marker in details for marker in ("private video", "video unavailable", "비공개", "사용할 수 없는 동영상")):
            code, message = "NOTEBOOKLM_VIDEO_UNAVAILABLE", "NotebookLM에서 사용할 수 없는 영상입니다."
        elif any(marker in details for marker in ("quota", "capacity", "source limit", "too many sources", "한도")):
            code, message = "NOTEBOOKLM_LIMIT_REACHED", "NotebookLM 사용 한도에 도달했습니다."
        elif any(marker in details for marker in ("not ready", "still processing", "processing source", "pending")):
            code, message = "NOTEBOOKLM_SOURCE_NOT_READY", "NotebookLM 소스 처리가 아직 끝나지 않았습니다."
        else:
            code, message = "NLM_PROCESSING_FAILED", "NotebookLM 명령 실행에 실패했습니다."
        raise NotebookLmCommandError(code, message)
    return completed.stdout.strip()


def extract_youtube_source_identities(raw_notebook: Any) -> dict[str, str]:
    """Return only unambiguous type-9 YouTube identities from raw metadata.

    The NotebookLM RPC schema is private and can change.  Its raw response is
    intentionally confined to the repo-owned helper; this function accepts it
    only so the helper and its contract tests can fail closed on schema drift.
    """
    if not isinstance(raw_notebook, list) or not raw_notebook:
        return {}
    notebook = raw_notebook[0] if isinstance(raw_notebook[0], list) else raw_notebook
    if not isinstance(notebook, list) or len(notebook) < 2 or not isinstance(notebook[1], list):
        return {}

    resolved: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in notebook[1]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        ids, metadata = row[0], row[2]
        source_id = ids[0] if isinstance(ids, list) and len(ids) == 1 else None
        if not isinstance(source_id, str) or not source_id or not isinstance(metadata, list):
            continue
        if len(metadata) <= 4 or metadata[4] != 9:
            continue

        current_present = len(metadata) > 5 and metadata[5] is not None
        current_url: str | None = None
        if current_present:
            current = metadata[5]
            if (
                not isinstance(current, list)
                or len(current) < 2
                or not isinstance(current[0], str)
                or not isinstance(current[1], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{11}", current[1])
            ):
                ambiguous.add(source_id)
                continue
            try:
                candidate = canonical_url(_normalize_scheme_less_youtube_locator(current[0]))
            except (KnowledgeError, ValueError):
                ambiguous.add(source_id)
                continue
            if youtube_video_id(candidate) != current[1]:
                ambiguous.add(source_id)
                continue
            current_url = candidate

        legacy_present = len(metadata) > 7 and metadata[7] is not None
        legacy_url: str | None = None
        if legacy_present:
            legacy = metadata[7]
            if not isinstance(legacy, list) or not legacy or not isinstance(legacy[0], str):
                ambiguous.add(source_id)
                continue
            try:
                candidate = canonical_url(legacy[0])
            except (KnowledgeError, ValueError):
                ambiguous.add(source_id)
                continue
            if not youtube_video_id(candidate):
                ambiguous.add(source_id)
                continue
            legacy_url = candidate

        selected = current_url or legacy_url
        if not selected or (current_url and legacy_url and current_url != legacy_url):
            if current_present or legacy_present:
                ambiguous.add(source_id)
            continue
        prior = resolved.get(source_id)
        if prior and prior != selected:
            ambiguous.add(source_id)
        else:
            resolved[source_id] = selected
    return {source_id: url for source_id, url in resolved.items() if source_id not in ambiguous}


def run_notebooklm_source_identity_helper(notebook_id: str, timeout_seconds: int = 120) -> dict[str, str]:
    """Run the pinned-package helper and retain only its minimal output."""
    helper = Path(__file__).resolve().parents[1] / "scripts" / "notebooklm_source_identity.py"
    command = [
        os.getenv("KNOWLEDGE_UVX_COMMAND", "uvx").strip() or "uvx",
        "--with",
        NOTEBOOKLM_PACKAGE_SPEC,
        "python",
        str(helper),
        notebook_id,
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, check=False, shell=False, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds,
            env=_child_process_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NotebookLmCommandError(
            "NLM_PROCESSING_FAILED", "NotebookLM source identity lookup failed."
        ) from error
    if completed.returncode != 0:
        raise NotebookLmCommandError(
            "NLM_PROCESSING_FAILED", "NotebookLM source identity lookup failed."
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise NotebookLmCommandError(
            "NLM_PROCESSING_FAILED", "NotebookLM source identity lookup failed."
        ) from error
    rows = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise NotebookLmCommandError(
            "NLM_PROCESSING_FAILED", "NotebookLM source identity lookup failed."
        )
    identities: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_id, url = row.get("source_id"), row.get("url")
        if not isinstance(source_id, str) or not source_id or not isinstance(url, str):
            continue
        try:
            canonical = canonical_url(url)
        except (KnowledgeError, ValueError):
            continue
        if not youtube_video_id(canonical) or source_id in identities:
            continue
        identities[source_id] = canonical
    return identities


@dataclass(frozen=True)
class NotebookInfo:
    notebook_id: str
    name: str


@dataclass(frozen=True)
class SourceInfo:
    notebook_id: str
    notebook_name: str
    source_id: str
    title: str
    url: str
    checked_at: str
    content_hash: str = ""
    first_seen_at: str = ""
    status: int | None = None
    source_type: str = ""

    @property
    def normalized_url(self) -> str:
        return canonical_url(self.url) if self.url else ""

    @property
    def video_id(self) -> str | None:
        return youtube_video_id(self.url)

    @property
    def key(self) -> str:
        if self.url:
            return source_key(self.url)
        return f"notebooklm:{self.notebook_id}:{self.source_id}"


@dataclass(frozen=True)
class CaptionCue:
    start_seconds: float
    end_seconds: float
    text: str


def _parse_vtt_clock(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError as error:
        raise CaptionEvidenceError(
            "YTDLP_CAPTION_FETCH_FAILED",
            "자막 시간 형식이 올바르지 않습니다.",
        ) from error
    raise CaptionEvidenceError(
        "YTDLP_CAPTION_FETCH_FAILED",
        "자막 시간 형식이 올바르지 않습니다.",
    )


def _clean_caption_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_HTML_TAG_PATTERN.sub("", value))).strip()


def _normalize_evidence_text(value: str) -> str:
    return "".join(
        character
        for character in _clean_caption_text(value).casefold()
        if character.isalnum()
    )


def _evidence_tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(
            r"[^\W_]+",
            _clean_caption_text(value).casefold(),
            re.UNICODE,
        )
        if token
    ]


_CRITICAL_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "thousand", "million", "billion", "trillion",
    "영", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구", "십", "백", "천", "만", "억", "조",
    "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉", "열",
}
_CRITICAL_NEGATIONS = {
    "no", "not", "never", "none", "neither", "without", "cannot", "cant", "isnt", "wasnt",
    "wont", "dont", "didnt", "없음", "없다", "없는", "아님", "아니다", "아닌", "안", "못",
}


def _raw_evidence_tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[^\W_]+", _clean_caption_text(value), re.UNICODE) if token]


def _critical_tokens(tokens: list[str]) -> Counter[str]:
    critical: Counter[str] = Counter()
    for index, token in enumerate(tokens):
        folded = token.casefold()
        has_number = any(character.isdigit() for character in token) or folded in _CRITICAL_NUMBER_WORDS
        is_negation = folded in _CRITICAL_NEGATIONS
        looks_like_name = (
            len(token) > 1
            and (
                token.isupper()
                or any(character.isupper() for character in token[1:])
                or (index > 0 and token[0].isupper())
            )
        )
        if has_number or is_negation or looks_like_name:
            critical[folded] += 1
    return critical


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"
    return f"[{minutes:02d}:{seconds:02d}]"


@dataclass(frozen=True)
class CaptionEvidence:
    cues: tuple[CaptionCue, ...]
    evidence_hash: str
    duration_seconds: float

    @classmethod
    def from_vtt(cls, raw: str) -> "CaptionEvidence":
        lines = raw.replace("\ufeff", "").splitlines()
        cues: list[CaptionCue] = []
        index = 0
        while index < len(lines):
            match = _VTT_TIMING_PATTERN.match(lines[index].strip())
            if not match:
                index += 1
                continue
            start = _parse_vtt_clock(match.group("start"))
            end = _parse_vtt_clock(match.group("end"))
            index += 1
            text_lines: list[str] = []
            while index < len(lines) and lines[index].strip():
                if _VTT_TIMING_PATTERN.match(lines[index].strip()):
                    break
                text_lines.append(lines[index])
                index += 1
            text = _clean_caption_text(" ".join(text_lines))
            if text and end > start:
                cue = CaptionCue(start, end, text)
                if (
                    not cues
                    or cue.text != cues[-1].text
                    or cue.start_seconds != cues[-1].start_seconds
                ):
                    cues.append(cue)
        if len(cues) < 3:
            raise CaptionEvidenceError(
                "YTDLP_CAPTION_UNAVAILABLE",
                "검증 가능한 공개 자막 구간이 부족합니다.",
            )
        digest_input = "\n".join(
            f"{cue.start_seconds:.3f}\t{cue.end_seconds:.3f}\t"
            f"{_normalize_evidence_text(cue.text)}"
            for cue in cues
        )
        return cls(
            tuple(cues),
            sha256_text(digest_input),
            max(cue.end_seconds for cue in cues),
        )

    def locate(self, quote: str) -> float:
        normalized_quote = _normalize_evidence_text(quote)
        if len(normalized_quote) < MIN_EVIDENCE_QUOTE_CHARS:
            raise EvidenceGroundingError(
                "NotebookLM 근거 문구가 검증하기에 너무 짧습니다."
            )
        stream_parts: list[str] = []
        positions: list[float] = []
        previous_end: float | None = None
        for cue in self.cues:
            normalized_cue = _normalize_evidence_text(cue.text)
            if not normalized_cue:
                continue
            current = "".join(stream_parts)
            overlap = 0
            if previous_end is not None and cue.start_seconds <= previous_end + 1.0:
                for size in range(
                    min(len(current), len(normalized_cue)),
                    0,
                    -1,
                ):
                    if current.endswith(normalized_cue[:size]):
                        overlap = size
                        break
            novel = normalized_cue[overlap:]
            stream_parts.append(novel)
            positions.extend([cue.start_seconds] * len(novel))
            previous_end = cue.end_seconds
        normalized_stream = "".join(stream_parts)
        position = normalized_stream.find(normalized_quote)
        if 0 <= position < len(positions):
            if normalized_stream.find(normalized_quote, position + 1) >= 0:
                raise EvidenceGroundingError(
                    "NotebookLM 근거 문구가 공개 자막의 여러 구간과 일치합니다."
                )
            return positions[position]

        quote_tokens = _evidence_tokens(quote)
        if len(quote_tokens) < 6:
            raise EvidenceGroundingError(
                "NotebookLM 근거 문구가 단어 기준으로 너무 짧습니다."
            )
        caption_tokens: list[str] = []
        caption_raw_tokens: list[str] = []
        token_positions: list[float] = []
        previous_end = None
        for cue in self.cues:
            raw_cue_tokens = _raw_evidence_tokens(cue.text)
            cue_tokens = [token.casefold() for token in raw_cue_tokens]
            if not cue_tokens:
                continue
            overlap = 0
            if previous_end is not None and cue.start_seconds <= previous_end + 1.0:
                for size in range(
                    min(len(caption_tokens), len(cue_tokens)),
                    0,
                    -1,
                ):
                    if caption_tokens[-size:] == cue_tokens[:size]:
                        overlap = size
                        break
            novel = cue_tokens[overlap:]
            caption_tokens.extend(novel)
            caption_raw_tokens.extend(raw_cue_tokens[overlap:])
            token_positions.extend(
                [cue.start_seconds] * len(novel)
            )
            previous_end = cue.end_seconds
        quote_raw_tokens = _raw_evidence_tokens(quote)
        quote_critical = _critical_tokens(quote_raw_tokens)
        candidates: list[tuple[float, int]] = []
        minimum = max(6, len(quote_tokens) - 2)
        maximum = len(quote_tokens) + 2
        for size in range(minimum, maximum + 1):
            for index in range(
                0,
                len(caption_tokens) - size + 1,
            ):
                window_raw = caption_raw_tokens[index : index + size]
                if _critical_tokens(window_raw) != quote_critical:
                    continue
                ratio = SequenceMatcher(
                    None,
                    quote_tokens,
                    caption_tokens[index : index + size],
                    autojunk=False,
                ).ratio()
                candidates.append((ratio, index))
        candidates.sort(reverse=True)
        best_ratio, best_index = candidates[0] if candidates else (0.0, -1)
        ambiguity_distance = max(3, len(quote_tokens) // 2)
        second_ratio = next(
            (ratio for ratio, index in candidates[1:] if abs(index - best_index) >= ambiguity_distance),
            0.0,
        )
        if (
            best_ratio < 0.85
            or (second_ratio >= 0.85 and best_ratio - second_ratio < 0.05)
            or best_index < 0
            or best_index >= len(token_positions)
        ):
            raise EvidenceGroundingError(
                "NotebookLM 근거 문구를 공개 자막에서 확인하지 못했습니다."
            )
        return token_positions[best_index]


CaptionCommandRunner = Callable[
    [list[str], int],
    subprocess.CompletedProcess[str],
]


def run_ytdlp_caption_command(
    args: list[str],
    timeout_seconds: int = 180,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=_child_process_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise CaptionEvidenceError(
            "YTDLP_CAPTION_FETCH_FAILED",
            "공개 자막 확인 시간이 초과됐습니다.",
        ) from error
    except OSError as error:
        raise CaptionEvidenceError(
            "YTDLP_CAPTION_FETCH_FAILED",
            f"공개 자막 도구를 실행하지 못했습니다: {safe_error(error)}",
        ) from error


class CaptionEvidenceProvider(Protocol):
    def fetch(self, url: str) -> CaptionEvidence: ...


class YoutubeCaptionProvider:
    """Read public captions in a self-deleting directory and return in-memory cues."""

    def __init__(
        self,
        runner: CaptionCommandRunner = run_ytdlp_caption_command,
    ) -> None:
        self._runner = runner

    def fetch(self, url: str) -> CaptionEvidence:
        normalized = canonical_url(url)
        if not youtube_video_id(normalized):
            raise CaptionEvidenceError(
                "YTDLP_VIDEO_UNAVAILABLE",
                "YouTube 영상 URL만 자막을 확인할 수 있습니다.",
            )
        with tempfile.TemporaryDirectory(
            prefix="yohan-knowledge-caption-"
        ) as temporary:
            root = Path(temporary).resolve()
            output_template = str(root / "cap.%(ext)s")
            last_failure_details = ""
            for languages in ("ko-orig,en-orig", "ko,en"):
                command = [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--ignore-config",
                    "--no-exec",
                    "--skip-download",
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-langs",
                    languages,
                    "--sub-format",
                    "vtt/best",
                    "--no-playlist",
                    "--output",
                    output_template,
                    normalized,
                ]
                completed = self._runner(command, 180)
                details = "\n".join(
                    part.strip()
                    for part in (completed.stderr, completed.stdout)
                    if part and part.strip()
                )
                lowered = details.casefold()
                if completed.returncode != 0:
                    last_failure_details = details
                    if any(
                        marker in lowered
                        for marker in (
                            "private video",
                            "video unavailable",
                            "sign in to confirm",
                            "비공개",
                        )
                    ):
                        raise CaptionEvidenceError(
                            "YTDLP_VIDEO_UNAVAILABLE",
                            "비공개이거나 사용할 수 없는 영상입니다.",
                        )
                    continue
                candidates: list[Path] = []
                for language in languages.split(","):
                    candidates.extend(
                        sorted(root.glob(f"cap.{language}.vtt"))
                    )
                if candidates:
                    try:
                        return CaptionEvidence.from_vtt(
                            candidates[0].read_text(encoding="utf-8")
                        )
                    except UnicodeError as error:
                        raise CaptionEvidenceError(
                            "YTDLP_CAPTION_FETCH_FAILED",
                            "공개 자막을 UTF-8로 읽지 못했습니다.",
                        ) from error

            lowered = last_failure_details.casefold()
            if last_failure_details and any(
                marker in lowered
                for marker in (
                    "429",
                    "too many requests",
                    "timed out",
                    "network",
                )
            ):
                raise CaptionEvidenceError(
                    "YTDLP_CAPTION_FETCH_FAILED",
                    "유튜브 응답 제한으로 공개 자막을 확인하지 못했습니다.",
                )
            if last_failure_details and not any(
                marker in lowered
                for marker in (
                    "subtitle",
                    "caption",
                    "requested subtitles",
                    "자막",
                )
            ):
                raise CaptionEvidenceError(
                    "YTDLP_CAPTION_FETCH_FAILED",
                    "공개 자막 확인에 실패했습니다: "
                    f"{safe_error(last_failure_details)}",
                )
            raise CaptionEvidenceError(
                "YTDLP_CAPTION_UNAVAILABLE",
                "공개 자막이 없는 영상입니다.",
            )


class NotebookLmClient:
    def __init__(
        self,
        runner: CommandRunner = run_notebooklm_command,
        source_identity_reader: SourceIdentityReader | None = None,
    ) -> None:
        self._runner = runner
        self._source_identity_reader = (
            run_notebooklm_source_identity_helper
            if runner is run_notebooklm_command and source_identity_reader is None
            else source_identity_reader
        )

    def _json(self, args: list[str], timeout: int = 120) -> Any:
        return _parse_json(self._runner(args, timeout))

    def list_notebooks(self) -> list[NotebookInfo]:
        value = self._json(["notebook", "list", "--json"])
        rows = _first_list(value, ("notebooks", "items", "data"))
        if rows is None:
            raise KnowledgeError("NotebookLM notebook list 계약을 확인하지 못했습니다.")
        result: list[NotebookInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            notebook_id = _first_string(row, ("id", "notebook_id", "notebookId"))
            if not notebook_id:
                continue
            name = _first_string(row, ("title", "name")) or notebook_id
            result.append(NotebookInfo(notebook_id, name))
        return result

    def list_sources(self, notebook: NotebookInfo, checked_at: str) -> list[SourceInfo]:
        value = self._json(["source", "list", notebook.notebook_id, "--json"])
        rows = _first_list(value, ("sources", "items", "data"))
        if rows is None:
            raise KnowledgeError("NotebookLM source list 계약을 확인하지 못했습니다.")
        identities: Mapping[str, str] = {}
        if self._source_identity_reader is not None:
            try:
                identities = self._source_identity_reader(notebook.notebook_id)
            except KnowledgeError:
                # Identity repair is optional and fails closed: never turn a
                # helper diagnostic into a registry value or operator output.
                identities = {}
        result: list[SourceInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_id = _first_string(row, ("id", "source_id", "sourceId"))
            url = _first_string(row, ("url", "source_url", "sourceUrl")) or ""
            if not source_id:
                continue
            if not url:
                url = identities.get(source_id, "")
            title = _first_string(row, ("title", "name")) or source_id
            source_type = _first_string(row, ("type", "source_type", "sourceType")) or ""
            if not source_type and youtube_video_id(url):
                source_type = "youtube"
            raw_status = row.get("status")
            if isinstance(raw_status, bool):
                status = None
            elif isinstance(raw_status, int):
                status = raw_status
            elif isinstance(raw_status, str) and raw_status.isdigit():
                status = int(raw_status)
            else:
                status = None
            result.append(
                SourceInfo(
                    notebook.notebook_id,
                    notebook.name,
                    source_id,
                    title,
                    url,
                    checked_at,
                    first_seen_at=checked_at,
                    status=status,
                    source_type=source_type,
                )
            )
        return result

    def add_youtube_source(self, notebook_id: str, url: str) -> str:
        value = self._json(
            ["source", "add", notebook_id, "--youtube", canonical_url(url), "--wait", "--json"],
            timeout=180,
        )
        source_id = _first_string(value, ("source_id", "sourceId", "id"))
        if not source_id:
            raise KnowledgeError("NotebookLM source add 결과에서 source ID를 찾지 못했습니다.")
        return source_id

    def get_source(self, source_id: str) -> str:
        try:
            raw = self._runner(["source", "get", source_id, "--json"], 120)
        except KnowledgeError:
            return self._runner(["source", "get", source_id], 120)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        content = _first_string(value, ("content", "text", "raw_text", "rawText"))
        if not content:
            raise KnowledgeError("NotebookLM source get 결과에서 원문을 찾지 못했습니다.")
        return content

    def query(self, notebook_id: str, source_id: str, prompt: str) -> str:
        value = self._json(
            ["notebook", "query", notebook_id, prompt, "--source-ids", source_id, "--json"],
            timeout=120,
        )
        text = _first_string(value, ("answer", "response", "text", "content", "message"))
        if not text:
            raise KnowledgeError("NotebookLM query 결과에서 답변을 찾지 못했습니다.")
        return text


@dataclass(frozen=True)
class RegistrySnapshot:
    checked_at: str
    sources: list[SourceInfo]


class NotebookRegistry:
    def __init__(self, path: Path | None = None, canonical_notebook_ids: Iterable[str] = ()) -> None:
        self.path = path or resolve_knowledge_runtime_dir() / "knowledge-registry.json"
        self.canonical_notebook_ids = tuple(item for item in canonical_notebook_ids if item)

    def _registry_lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(self.path.with_suffix(".lock"))

    def source_lock(self, url: str) -> InterProcessFileLock:
        digest = sha256_text(source_key(url))
        return InterProcessFileLock(self.path.parent / "locks" / f"source-{digest}.lock")

    def load(self) -> RegistrySnapshot | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            rows = value.get("sources", []) if isinstance(value, dict) else []
            checked_at = value.get("checked_at", "") if isinstance(value, dict) else ""
            sources = [SourceInfo(**row) for row in rows if isinstance(row, dict)]
            return RegistrySnapshot(str(checked_at), sources)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise KnowledgeError(f"NotebookLM registry를 읽지 못했습니다: {safe_error(error)}") from error

    def _save_unlocked(self, snapshot: RegistrySnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "checked_at": snapshot.checked_at,
            "sources": [asdict(source) for source in snapshot.sources],
        }
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def save(self, snapshot: RegistrySnapshot) -> None:
        with self._registry_lock():
            self._save_unlocked(snapshot)

    def fresh(self, now: datetime | None = None) -> bool:
        snapshot = self.load()
        if not snapshot or not snapshot.checked_at:
            return False
        try:
            checked = datetime.fromisoformat(snapshot.checked_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return (now or utc_now()) - checked <= REGISTRY_TTL

    def refresh(
        self,
        notebooklm: NotebookLmClient,
        notebook_ids: Iterable[str] = (),
        now: datetime | None = None,
    ) -> RegistrySnapshot:
        with self._registry_lock():
            checked_at = isoformat(now or utc_now())
            previous = self.load()
            previous_sources = previous.sources if previous else []
            previous_hashes = {
                (source.notebook_id, source.source_id): source.content_hash
                for source in previous_sources
                if source.content_hash
            }
            previous_first_seen = {
                (source.notebook_id, source.source_id): source.first_seen_at or source.checked_at
                for source in previous_sources
            }
            previous_statuses = {
                (source.notebook_id, source.source_id): source.status
                for source in previous_sources
                if source.status is not None
            }
            previous_urls = {
                (source.notebook_id, source.source_id): source.url
                for source in previous_sources
                if source.url
            }
            previous_types = {
                (source.notebook_id, source.source_id): source.source_type
                for source in previous_sources
                if source.source_type
            }
            ids = [item for item in notebook_ids if item]
            if ids:
                notebooks = [NotebookInfo(item, item) for item in ids]
            else:
                notebooks = notebooklm.list_notebooks()
            sources: list[SourceInfo] = []
            for notebook in notebooks:
                for source in notebooklm.list_sources(notebook, checked_at):
                    sources.append(
                        SourceInfo(
                            source.notebook_id,
                            source.notebook_name,
                            source.source_id,
                            source.title,
                            source.url
                            or previous_urls.get((source.notebook_id, source.source_id), ""),
                            source.checked_at,
                            previous_hashes.get((source.notebook_id, source.source_id), ""),
                            previous_first_seen.get(
                                (source.notebook_id, source.source_id),
                                source.first_seen_at or source.checked_at,
                            ),
                            source.status
                            if source.status is not None
                            else previous_statuses.get((source.notebook_id, source.source_id)),
                            source.source_type
                            or previous_types.get((source.notebook_id, source.source_id), ""),
                        )
                    )
            snapshot = RegistrySnapshot(checked_at, sources)
            self._save_unlocked(snapshot)
            return snapshot

    def append(self, source: SourceInfo) -> None:
        with self._registry_lock():
            snapshot = self.load() or RegistrySnapshot(source.checked_at, [])
            existing = next(
                (
                    item
                    for item in snapshot.sources
                    if item.notebook_id == source.notebook_id and item.source_id == source.source_id
                ),
                None,
            )
            remaining = [
                item
                for item in snapshot.sources
                if not (item.notebook_id == source.notebook_id and item.source_id == source.source_id)
            ]
            merged = SourceInfo(
                source.notebook_id,
                source.notebook_name,
                source.source_id,
                source.title,
                source.url or (existing.url if existing else ""),
                source.checked_at,
                source.content_hash or (existing.content_hash if existing else ""),
                source.first_seen_at
                or ((existing.first_seen_at or existing.checked_at) if existing else source.checked_at),
                source.status if source.status is not None else (existing.status if existing else None),
                source.source_type or (existing.source_type if existing else ""),
            )
            self._save_unlocked(RegistrySnapshot(source.checked_at, [*remaining, merged]))

    def find(self, url: str) -> list[SourceInfo]:
        expected = source_key(url)
        snapshot = self.load()
        if not snapshot:
            return []
        found = [source for source in snapshot.sources if source.key == expected]
        canonical_order = {value: index for index, value in enumerate(self.canonical_notebook_ids)}
        def first_seen_epoch(source: SourceInfo) -> float:
            try:
                value = source.first_seen_at or source.checked_at
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except (ValueError, OverflowError):
                return 0.0

        return sorted(
            found,
            key=lambda source: (
                source.status not in (None, 2),
                source.notebook_name.upper().startswith("ARCHIVE"),
                canonical_order.get(source.notebook_id, len(canonical_order)),
                -first_seen_epoch(source),
                source.notebook_id,
                source.source_id,
            ),
        )

    def duplicate_groups(self) -> list[dict[str, Any]]:
        snapshot = self.load()
        groups: dict[str, list[SourceInfo]] = {}
        for source in snapshot.sources if snapshot else []:
            if not source.url:
                continue
            groups.setdefault(source.key, []).append(source)
        result: list[dict[str, Any]] = []
        for key, items in sorted(groups.items()):
            if len(items) <= 1:
                continue
            ordered = self.find(items[0].url)
            result.append(
                {
                    "source_key": key,
                    "preferred": asdict(ordered[0]),
                    "duplicates": [asdict(item) for item in ordered[1:]],
                    "auto_deleted": False,
                }
            )
        return result

    def unresolved_youtube_title_matches(self, title: str) -> list[SourceInfo]:
        expected = re.sub(r"\s+", " ", title).strip().casefold()
        if not expected:
            return []
        snapshot = self.load()
        return [
            source
            for source in (snapshot.sources if snapshot else [])
            if not source.url
            and source.source_type.casefold() == "youtube"
            and re.sub(r"\s+", " ", source.title).strip().casefold() == expected
        ]

    def unresolved_youtube_sources(self) -> list[SourceInfo]:
        snapshot = self.load()
        return [
            source
            for source in (snapshot.sources if snapshot else [])
            if not source.url and source.source_type.casefold() == "youtube"
        ]


class Queue(Protocol):
    def claim(self, worker_id: str, limit: int) -> list[dict[str, Any]]: ...
    def retry(self, job_id: str) -> dict[str, Any]: ...
    def invalidate_review(self, job_id: str) -> dict[str, Any]: ...
    def checkpoint(self, job: Mapping[str, Any], **fields: Any) -> dict[str, Any]: ...
    def complete(self, job: Mapping[str, Any], status: str, **fields: Any) -> dict[str, Any]: ...
    def reviews(self) -> list[dict[str, Any]]: ...
    def get(self, job_id: str) -> dict[str, Any]: ...
    def begin_approval(self, job: Mapping[str, Any], intent_hash: str) -> dict[str, Any]: ...
    def mark_completed(self, job: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]: ...
    def defer(self, job: Mapping[str, Any]) -> dict[str, Any]: ...
    def reject(self, job: Mapping[str, Any]) -> dict[str, Any]: ...


class FocusFeedQueue:
    def __init__(
        self,
        url: str,
        service_role_key: str,
        owner_user_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlsplit(url.strip().rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise KnowledgeError("FOCUS_FEED_SUPABASE_URL은 http(s) URL이어야 합니다.")
        if not service_role_key.strip():
            raise KnowledgeError("FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY가 없습니다.")
        if not _UUID_PATTERN.fullmatch(owner_user_id.strip()):
            raise KnowledgeError("FOCUS_FEED_OWNER_USER_ID는 auth.users UUID여야 합니다.")
        self.url = url.strip().rstrip("/")
        self.key = service_role_key.strip()
        self.owner_user_id = owner_user_id.strip().lower()
        self.client = client or httpx.Client(timeout=30.0)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FocusFeedQueue":
        source = env or os.environ
        missing = [
            key
            for key in (
                "FOCUS_FEED_SUPABASE_URL",
                "FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY",
                "FOCUS_FEED_OWNER_USER_ID",
            )
            if not source.get(key, "").strip()
        ]
        if missing:
            raise KnowledgeError(f"Focus Feed 대기열 설정이 없습니다: {', '.join(missing)}")
        return cls(
            source["FOCUS_FEED_SUPABASE_URL"],
            source["FOCUS_FEED_SUPABASE_SERVICE_ROLE_KEY"],
            source["FOCUS_FEED_OWNER_USER_ID"],
        )

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if not self.key.startswith("sb_"):
            headers["Authorization"] = f"Bearer {self.key}"
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(self, method: str, path: str, *, body: Any = None, prefer: str | None = None) -> Any:
        response = self.client.request(
            method,
            f"{self.url}{path}",
            headers=self._headers(prefer),
            json=body,
        )
        if response.status_code >= 400:
            message = safe_error(response.text, 500)
            if "job lease is no longer valid" in message.lower():
                raise LeaseLostError()
            raise KnowledgeError(f"Focus Feed queue HTTP {response.status_code}: {message}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise KnowledgeError("Focus Feed queue 응답이 JSON이 아닙니다.") from error

    @staticmethod
    def _single(value: Any, operation: str) -> dict[str, Any]:
        row = value[0] if isinstance(value, list) and len(value) == 1 else value
        if not isinstance(row, dict):
            raise KnowledgeError(f"{operation} 응답에서 단일 작업을 찾지 못했습니다.")
        return row

    def claim(self, worker_id: str, limit: int) -> list[dict[str, Any]]:
        value = self._request(
            "POST",
            "/rest/v1/rpc/claim_knowledge_jobs",
            body={
                "p_user_id": self.owner_user_id,
                "p_worker_id": worker_id[:120],
                "p_limit": max(1, min(limit, MAX_BATCH)),
                "p_lease_seconds": LEASE_SECONDS,
            },
        )
        if not isinstance(value, list):
            raise KnowledgeError("claim 응답이 배열이 아닙니다.")
        return [row for row in value if isinstance(row, dict)]

    def retry(self, job_id: str) -> dict[str, Any]:
        return self._single(
            self._request(
                "POST",
                "/rest/v1/rpc/retry_knowledge_job",
                body={
                    "p_user_id": self.owner_user_id,
                    "p_job_id": job_id,
                },
            ),
            "retry",
        )

    def invalidate_review(self, job_id: str) -> dict[str, Any]:
        return self._single(
            self._request(
                "POST",
                "/rest/v1/rpc/invalidate_knowledge_review",
                body={
                    "p_user_id": self.owner_user_id,
                    "p_job_id": job_id,
                },
            ),
            "invalidate review",
        )

    def checkpoint(self, job: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
        lease_token = str(job.get("lease_token") or "")
        if not lease_token:
            raise LeaseLostError("lease token 없는 작업은 갱신할 수 없습니다.")
        body = {
            "p_user_id": self.owner_user_id,
            "p_job_id": job.get("id"),
            "p_lease_token": lease_token,
            "p_notebook_id": fields.get("notebook_id"),
            "p_notebook_name": fields.get("notebook_name"),
            "p_notebook_source_id": fields.get("notebook_source_id"),
            "p_notebook_source_added_at": fields.get("notebook_source_added_at"),
            "p_source_hash": fields.get("source_hash"),
            "p_transcript_hash": fields.get("transcript_hash"),
            "p_lease_seconds": LEASE_SECONDS,
        }
        return self._single(self._request("POST", "/rest/v1/rpc/checkpoint_knowledge_job", body=body), "checkpoint")

    def complete(self, job: Mapping[str, Any], status: str, **fields: Any) -> dict[str, Any]:
        if status not in {"review_required", "action_required"}:
            raise KnowledgeError(f"지원하지 않는 완료 상태입니다: {status}")
        body = {
            "p_user_id": self.owner_user_id,
            "p_job_id": job.get("id"),
            "p_lease_token": job.get("lease_token"),
            "p_status": status,
            "p_result": fields.get("result", {}),
            "p_quality_score": fields.get("quality_score"),
            "p_quality_report": fields.get("quality_report", {}),
            "p_failure_code": fields.get("failure_code"),
            "p_failure_message": fields.get("failure_message"),
        }
        return self._single(self._request("POST", "/rest/v1/rpc/complete_knowledge_job", body=body), "complete")

    def get(self, job_id: str) -> dict[str, Any]:
        value = self._request(
            "GET",
            f"/rest/v1/knowledge_jobs?id=eq.{job_id}&user_id=eq.{self.owner_user_id}&select=*",
        )
        if not isinstance(value, list) or not value or not isinstance(value[0], dict):
            raise KnowledgeError(f"작업을 찾을 수 없습니다: {job_id}")
        return value[0]

    def reviews(self) -> list[dict[str, Any]]:
        value = self._request(
            "GET",
            f"/rest/v1/knowledge_jobs?user_id=eq.{self.owner_user_id}&status=in.(review_required,approving)&select=*&order=created_at.asc",
        )
        if not isinstance(value, list):
            raise KnowledgeError("검토 목록 응답이 배열이 아닙니다.")
        return [row for row in value if isinstance(row, dict)]

    def _patch_review(self, job: Mapping[str, Any], fields: Mapping[str, Any], status_filter: str = "review_required") -> dict[str, Any]:
        value = self._request(
            "PATCH",
            f"/rest/v1/knowledge_jobs?id=eq.{job.get('id')}&user_id=eq.{self.owner_user_id}&status=eq.{status_filter}",
            body=dict(fields),
            prefer="return=representation",
        )
        if not isinstance(value, list) or not value or not isinstance(value[0], dict):
            raise KnowledgeError("작업 상태가 바뀌어 요청을 적용하지 못했습니다.")
        return value[0]

    def begin_approval(self, job: Mapping[str, Any], intent_hash: str) -> dict[str, Any]:
        body = {
            "p_user_id": self.owner_user_id,
            "p_job_id": job.get("id"),
            "p_intent_hash": intent_hash,
        }
        return self._single(
            self._request("POST", "/rest/v1/rpc/begin_knowledge_approval", body=body),
            "begin approval",
        )

    def mark_completed(self, job: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        token = str(job.get("approval_token") or "")
        if not token:
            raise KnowledgeError("approval token 없는 작업은 완료할 수 없습니다.")
        return self._single(
            self._request(
                "POST",
                "/rest/v1/rpc/complete_knowledge_approval",
                body={
                    "p_user_id": self.owner_user_id,
                    "p_job_id": job.get("id"),
                    "p_approval_token": token,
                    "p_result": dict(result),
                },
            ),
            "complete approval",
        )

    def defer(self, job: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(job.get("result") or {})
        result["review_decision"] = "deferred"
        return self._patch_review(job, {"result": result})

    def reject(self, job: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(job.get("result") or {})
        result["review_decision"] = "rejected"
        return self._patch_review(job, {"status": "cancelled", "result": result})


def _json_candidates(raw: str) -> Iterable[str]:
    yield raw.strip()
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE):
        yield match.group(1).strip()
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        yield raw[first : last + 1]


def parse_draft(raw: str) -> dict[str, Any] | None:
    for candidate in _json_candidates(raw):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


_DRAFT_TOP_LEVEL_FIELDS = {
    "title",
    "summary",
    "key_points",
    "claims",
    "coverage",
    "yohan_relevance",
    "uncertainties",
    "promotion_candidates",
}
_CLAIM_FIELDS = {
    "type",
    "statement",
    "evidence_quote",
    "caption_quote",
    "citation",
    "citation_verified",
    "requires_crosscheck",
}
_COVERAGE_ITEM_FIELDS = {
    "statement",
    "evidence_quote",
    "caption_quote",
    "citation",
    "citation_verified",
}


def _contract_text(value: Any, *, maximum: int = 12_000) -> str:
    if not isinstance(value, str):
        raise DraftContractError("NotebookLM 응답의 문자열 필드 형식이 올바르지 않습니다.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise DraftContractError("NotebookLM 응답의 문자열 필드 길이가 올바르지 않습니다.")
    return cleaned


def _contract_text_list(value: Any, *, maximum_items: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise DraftContractError("NotebookLM 응답의 문자열 목록 형식이 올바르지 않습니다.")
    return [_contract_text(item, maximum=4_000) for item in value]


def _sanitize_draft_contract(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an exact allowlist and rebuild the only persistable draft shape."""
    if set(draft) != _DRAFT_TOP_LEVEL_FIELDS:
        raise DraftContractError("NotebookLM 응답에 누락되거나 허용되지 않은 최상위 필드가 있습니다.")

    raw_claims = draft.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims or len(raw_claims) > 64:
        raise DraftContractError("NotebookLM 주장 목록 형식이 올바르지 않습니다.")
    claims: list[dict[str, Any]] = []
    for raw_claim in raw_claims:
        if not isinstance(raw_claim, dict) or not set(raw_claim).issubset(_CLAIM_FIELDS):
            raise DraftContractError("NotebookLM 주장에 허용되지 않은 필드가 있습니다.")
        claim_type = _contract_text(raw_claim.get("type"), maximum=32)
        if claim_type not in {"fact", "interpretation", "recommendation"}:
            raise DraftContractError("NotebookLM 주장 유형이 올바르지 않습니다.")
        requires_crosscheck = raw_claim.get("requires_crosscheck", False)
        if not isinstance(requires_crosscheck, bool):
            raise DraftContractError("NotebookLM 교차검증 필드 형식이 올바르지 않습니다.")
        quote_value = raw_claim.get("evidence_quote") or raw_claim.get("caption_quote")
        quote = _contract_text(quote_value, maximum=2_000) if quote_value else ""
        if claim_type == "fact" and not quote:
            raise DraftContractError("NotebookLM 사실 주장에 근거 문구가 없습니다.")
        claims.append(
            {
                "type": claim_type,
                "statement": _contract_text(raw_claim.get("statement"), maximum=4_000),
                "evidence_quote": quote,
                "requires_crosscheck": requires_crosscheck,
            }
        )

    raw_coverage = draft.get("coverage")
    if not isinstance(raw_coverage, dict) or set(raw_coverage) != {"start", "middle", "end"}:
        raise DraftContractError("NotebookLM 영상 구간 필드가 올바르지 않습니다.")
    coverage: dict[str, dict[str, str]] = {}
    for part in ("start", "middle", "end"):
        raw_item = raw_coverage.get(part)
        if not isinstance(raw_item, dict) or not set(raw_item).issubset(_COVERAGE_ITEM_FIELDS):
            raise DraftContractError("NotebookLM 영상 구간에 허용되지 않은 필드가 있습니다.")
        quote_value = raw_item.get("evidence_quote") or raw_item.get("caption_quote")
        coverage[part] = {
            "statement": _contract_text(raw_item.get("statement"), maximum=4_000),
            "evidence_quote": _contract_text(quote_value, maximum=2_000),
        }

    promotion = draft.get("promotion_candidates")
    if not isinstance(promotion, dict) or set(promotion) != {"concepts", "people", "triples"}:
        raise DraftContractError("NotebookLM 승격 후보 필드가 올바르지 않습니다.")
    if any(not isinstance(promotion.get(key), list) for key in ("concepts", "people", "triples")):
        raise DraftContractError("NotebookLM 승격 후보 목록 형식이 올바르지 않습니다.")

    return {
        "title": _contract_text(draft.get("title"), maximum=1_000),
        "summary": _contract_text(draft.get("summary")),
        "key_points": _contract_text_list(draft.get("key_points")),
        "claims": claims,
        "coverage": coverage,
        "yohan_relevance": _contract_text(draft.get("yohan_relevance"), maximum=8_000),
        "uncertainties": _contract_text_list(draft.get("uncertainties")),
        # P0 never promotes model-proposed entities automatically.
        "promotion_candidates": {"concepts": [], "people": [], "triples": []},
    }


def ground_draft_with_caption_evidence(
    draft: Mapping[str, Any],
    evidence: CaptionEvidence,
    source_text: str | None = None,
) -> dict[str, Any]:
    """Replace model timestamps with verified caption positions and strip quotes."""
    sanitized = _sanitize_draft_contract(draft)
    grounded = {
        "title": sanitized["title"],
        "summary": sanitized["summary"],
        "key_points": sanitized["key_points"],
        "yohan_relevance": sanitized["yohan_relevance"],
        "uncertainties": sanitized["uncertainties"],
        "promotion_candidates": sanitized["promotion_candidates"],
    }
    normalized_source = (
        _normalize_evidence_text(source_text)
        if source_text is not None
        else ""
    )

    def locate(quote: str) -> float:
        normalized_quote = _normalize_evidence_text(quote)
        if (
            source_text is not None
            and normalized_quote not in normalized_source
        ):
            raise EvidenceGroundingError(
                "NotebookLM 근거 문구가 NotebookLM 원문에 존재하지 않습니다."
            )
        return evidence.locate(quote)

    raw_claims = sanitized["claims"]
    claims: list[dict[str, Any]] = []
    for raw_claim in raw_claims:
        quote = str(raw_claim.get("evidence_quote") or "")
        claim = {
            "type": raw_claim["type"],
            "statement": raw_claim["statement"],
            "requires_crosscheck": raw_claim["requires_crosscheck"],
        }
        if claim.get("type") == "fact":
            timestamp = locate(quote)
            claim["evidence_quote"] = quote
            claim["citation"] = _format_timestamp(timestamp)
            claim["citation_verified"] = True
        claims.append(claim)
    grounded["claims"] = claims

    raw_coverage = sanitized["coverage"]
    coverage: dict[str, dict[str, Any]] = {}
    for part, lower, upper in (
        ("start", 0.0, 1.0 / 3.0),
        ("middle", 1.0 / 3.0, 2.0 / 3.0),
        ("end", 2.0 / 3.0, 1.000001),
    ):
        raw_item = raw_coverage.get(part)
        statement = raw_item["statement"]
        quote = raw_item["evidence_quote"]
        timestamp = locate(quote)
        ratio = timestamp / max(evidence.duration_seconds, 0.001)
        if not (lower <= ratio < upper):
            raise EvidenceGroundingError(
                f"NotebookLM {part} 근거가 해당 영상 구간에 있지 않습니다."
            )
        coverage[part] = {
            "statement": statement,
            "evidence_quote": quote,
            "citation": _format_timestamp(timestamp),
            "citation_verified": True,
        }
    grounded["coverage"] = coverage
    return grounded


def ground_draft_with_source_evidence(
    draft: Mapping[str, Any], source_text: str, source_id: str,
) -> dict[str, Any]:
    """Ground a draft exclusively in an already-selected NotebookLM source.

    This fail-closed fallback invokes no URL fetcher, browser, or transcript
    downloader. Every quote must appear once in the ``source get`` response
    and start/middle/end must occupy distinct source thirds. Character
    positions are not video timestamps, so this contract cannot pass the
    review quality gate without separately verified public-caption evidence.
    """
    sanitized = _sanitize_draft_contract(draft)
    normalized_source = _normalize_evidence_text(source_text)
    if not normalized_source:
        raise EvidenceGroundingError("NotebookLM source get 결과가 비어 있습니다.")

    def locate(quote: str) -> tuple[str, int]:
        cleaned = _contract_text(quote, maximum=2_000)
        normalized_quote = _normalize_evidence_text(cleaned)
        index = normalized_source.find(normalized_quote)
        if index < 0:
            raise EvidenceGroundingError("NotebookLM source get 원문에 근거 문구가 없습니다.")
        if normalized_source.find(normalized_quote, index + 1) >= 0:
            raise EvidenceGroundingError(
                "NotebookLM source get 근거 문구가 여러 위치에 있어 구간을 확인할 수 없습니다."
            )
        return cleaned, index

    claims: list[dict[str, Any]] = []
    for item in sanitized["claims"]:
        claim = {
            "type": item["type"],
            "statement": item["statement"],
            "requires_crosscheck": item["requires_crosscheck"],
        }
        if item["type"] == "fact":
            quote, index = locate(item["evidence_quote"])
            claim.update(
                evidence_quote=quote,
                citation=f"NotebookLM source_get:{source_id}#char={index}",
                citation_verified=True,
            )
        claims.append(claim)
    coverage: dict[str, dict[str, Any]] = {}
    source_length = max(len(normalized_source), 1)
    for part, lower, upper in (
        ("start", 0.0, 1.0 / 3.0),
        ("middle", 1.0 / 3.0, 2.0 / 3.0),
        ("end", 2.0 / 3.0, 1.000001),
    ):
        item = sanitized["coverage"][part]
        quote, index = locate(item["evidence_quote"])
        ratio = index / source_length
        if not (lower <= ratio < upper):
            raise EvidenceGroundingError(
                f"NotebookLM {part} 근거가 해당 원문 구간에 있지 않습니다."
            )
        coverage[part] = {
            "statement": item["statement"],
            "evidence_quote": quote,
            "citation": f"NotebookLM source_get:{source_id}#char={index}",
            "citation_verified": True,
        }
    return {
        "title": sanitized["title"],
        "summary": sanitized["summary"],
        "key_points": sanitized["key_points"],
        "claims": claims,
        "coverage": coverage,
        "yohan_relevance": sanitized["yohan_relevance"],
        "uncertainties": sanitized["uncertainties"],
        "promotion_candidates": sanitized["promotion_candidates"],
    }


def evaluate_draft(
    draft: Mapping[str, Any], transcript_evidence: bool, tier: str = "T2", *,
    evidence_contract: str = "caption-v1",
) -> dict[str, Any]:
    hard_failures: list[str] = []
    warnings: list[str] = []
    summary = str(draft.get("summary") or "").strip()
    key_points = draft.get("key_points") if isinstance(draft.get("key_points"), list) else []
    claims = draft.get("claims") if isinstance(draft.get("claims"), list) else []
    coverage = draft.get("coverage") if isinstance(draft.get("coverage"), dict) else {}
    uncertainties = draft.get("uncertainties") if isinstance(draft.get("uncertainties"), list) else []
    relevance = str(draft.get("yohan_relevance") or "").strip()
    coverage_count = sum(
        isinstance(coverage.get(part), dict)
        and bool(str(coverage[part].get("statement") or "").strip())
        and coverage[part].get("citation_verified") is True
        and bool(_TIMESTAMP_PATTERN.search(str(coverage[part].get("citation") or "")))
        for part in ("start", "middle", "end")
    )
    factual = [claim for claim in claims if isinstance(claim, dict) and claim.get("type") == "fact"]
    cited = [
        claim
        for claim in factual
        if claim.get("citation_verified") is True
        and _TIMESTAMP_PATTERN.search(str(claim.get("citation") or ""))
    ]
    if tier != "T1" and not transcript_evidence:
        hard_failures.append("유효한 자막·타임스탬프 근거를 확인하지 못했습니다.")
    if len(summary) < 80:
        hard_failures.append("요약이 너무 짧거나 없습니다.")
    if len(key_points) < 3:
        hard_failures.append("핵심 요점이 3개 미만입니다.")
    if coverage_count < 3:
        hard_failures.append("영상 시작·중간·끝 커버리지가 모두 필요합니다.")
    if not claims:
        hard_failures.append("사실·해석·권고를 분리한 주장 목록이 필요합니다.")
    if len(cited) < len(factual):
        hard_failures.append("모든 사실 주장에는 타임스탬프 인용이 필요합니다.")
    if not uncertainties:
        warnings.append("불확실성이 없더라도 '없음'이라고 명시해야 합니다.")
    dimensions = {
        "fidelity": 30 if transcript_evidence else 0,
        "coverage": round(coverage_count / 3 * 20),
        "citations": 15 if not factual else round(len(cited) / len(factual) * 15),
        "concision": 10 if 120 <= len(summary) <= 4500 else 5,
        "feynman": 10 if len(key_points) >= 3 else 0,
        "yohan_relevance": 10 if len(relevance) >= 20 else 0,
        "uncertainty": 5 if uncertainties else 0,
    }
    score = sum(dimensions.values())
    return {
        "score": score,
        "dimensions": dimensions,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "requires_second_evaluator": tier == "T3",
        "evidence_contract": evidence_contract,
        "passed": not hard_failures and score >= QUALITY_THRESHOLD,
    }


def build_query_prompt(job: Mapping[str, Any]) -> str:
    skeleton = {
        "title": "영상 제목",
        "summary": "원문 사실과 해석을 구분한 문단형 요약",
        "key_points": ["핵심 1", "핵심 2", "핵심 3"],
        "claims": [
            {
                "type": "fact",
                "statement": "사실",
                "evidence_quote": "source에서 그대로 복사한 짧은 연속 원문 구절",
                "requires_crosscheck": False,
            }
        ],
        "coverage": {
            "start": {
                "statement": "시작 구간 설명",
                "evidence_quote": "시작 구간에서 그대로 복사한 짧은 원문 구절",
            },
            "middle": {
                "statement": "중간 구간 설명",
                "evidence_quote": "중간 구간에서 그대로 복사한 짧은 원문 구절",
            },
            "end": {
                "statement": "끝 구간 설명",
                "evidence_quote": "끝 구간에서 그대로 복사한 짧은 원문 구절",
            },
        },
        "yohan_relevance": "요한의 1인 AI 운영 적용점",
        "uncertainties": ["없음"],
        "promotion_candidates": {"concepts": [], "people": [], "triples": []},
    }
    return "\n".join(
        (
            "당신은 근거 우선 한국어 지식 분석가입니다.",
            f"이 YouTube source 하나만 분석하세요: {job.get('source_url')}",
            f"영상 제목: {job.get('title')}",
            "다른 notebook source를 섞지 마세요.",
            "타임스탬프를 만들거나 추측하지 마세요. 시스템이 공개 자막에서 시간을 검증합니다.",
            "모든 fact 주장에는 source에 연속 등장하는 8~20단어 evidence_quote를 넣으세요.",
            "evidence_quote는 번역·생략·이어붙이기 없이 원래 문구를 짧게 그대로 복사하세요.",
            "시작·중간·끝 coverage도 statement와 실제 evidence_quote로 작성하세요.",
            "fact/interpretation/recommendation을 분리하세요.",
            "설명문 없이 아래 JSON 구조만 반환하세요.",
            json.dumps(skeleton, ensure_ascii=False),
        )
    )


def build_grounding_repair_prompt(
    job: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> str:
    return "\n".join(
        (
            "아래 JSON의 요약 내용은 유지하고 evidence_quote만 교정하세요.",
            f"이 YouTube source 하나만 사용하세요: {job.get('source_url')}",
            "각 evidence_quote는 source에서 번역·생략·이어붙이기 없이 그대로 복사한 8~15단어여야 합니다.",
            "coverage.start는 source의 첫 1/3, middle은 가운데 1/3, end는 마지막 1/3에서 복사하세요.",
            "타임스탬프를 만들지 마세요.",
            "Markdown fence·설명·각주 없이 유효한 JSON 객체 하나만 반환하세요.",
            json.dumps(dict(draft), ensure_ascii=False),
        )
    )


class ReviewStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or resolve_knowledge_runtime_dir() / "reviews"

    def write(self, job_id: str, value: Mapping[str, Any]) -> Path:
        if not _UUID_PATTERN.fullmatch(job_id):
            raise KnowledgeError("job ID가 UUID 형식이 아닙니다.")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{job_id}.json"
        content = json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True)
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise KnowledgeError("기존 검토 후보와 새 결과가 달라 덮어쓰지 않았습니다.")
            return path
        path.write_text(content, encoding="utf-8")
        return path

    def write_source_text(self, job_id: str, content: str) -> Path:
        if not _UUID_PATTERN.fullmatch(job_id):
            raise KnowledgeError("job ID가 UUID 형식이 아닙니다.")
        if not content.strip():
            raise KnowledgeError("비어 있는 NotebookLM 원문은 staging할 수 없습니다.")
        root = self.root.parent / "transcripts"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{job_id}.txt"
        if path.exists():
            if sha256_text(path.read_text(encoding="utf-8")) != sha256_text(content):
                raise KnowledgeError("기존 로컬 원문 staging과 새 원문이 달라 덮어쓰지 않았습니다.")
            return path
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except FileExistsError:
            if sha256_text(path.read_text(encoding="utf-8")) != sha256_text(content):
                raise KnowledgeError("동시 원문 staging 내용이 다릅니다.")
        return path

    def write_approval_intent(
        self,
        job_id: str,
        intent_hash: str,
        human_note: str,
    ) -> Path:
        if not _UUID_PATTERN.fullmatch(job_id) or not re.fullmatch(r"[0-9a-f]{64}", intent_hash):
            raise KnowledgeError("approval intent ID 또는 hash 형식이 올바르지 않습니다.")
        path = self.root.parent / "approval-intents" / f"{job_id}.json"
        content = json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "intent_hash": intent_hash,
                "human_note": human_note,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        atomic_write_once(path, content)
        return path

    def read_approval_intent(self, job_id: str) -> dict[str, str] | None:
        if not _UUID_PATTERN.fullmatch(job_id):
            raise KnowledgeError("job ID가 UUID 형식이 아닙니다.")
        path = self.root.parent / "approval-intents" / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeError("로컬 approval intent를 읽지 못했습니다.") from error
        if not isinstance(value, dict):
            raise KnowledgeError("로컬 approval intent 형식이 올바르지 않습니다.")
        intent_hash = str(value.get("intent_hash") or "")
        human_note = str(value.get("human_note") or "")
        if value.get("job_id") != job_id or sha256_text(human_note) != intent_hash:
            raise KnowledgeError("로컬 approval intent가 손상되었습니다.")
        return {"intent_hash": intent_hash, "human_note": human_note}


class BrainWriter:
    def __init__(self, brain_root: Path | None = None) -> None:
        configured = brain_root or (Path(os.environ["YOHAN_BRAIN_ROOT"]) if os.getenv("YOHAN_BRAIN_ROOT") else None)
        if configured is None:
            raise KnowledgeError("YOHAN_BRAIN_ROOT가 없어 승인 적재를 실행할 수 없습니다.")
        # Keep the lexical path as well as the resolved path.  ``resolve()``
        # alone is unsafe: it silently accepts a root or parent junction that
        # redirects a write outside the checked-out Brain repository.
        self.root = Path(os.path.abspath(str(configured)))
        self._assert_safe_existing_chain(self.root)
        self._real_root = Path(os.path.realpath(self.root))
        if not (self.root / "memory").is_dir():
            raise KnowledgeError("YOHAN_BRAIN_ROOT가 brain 저장소가 아닙니다: memory/가 없습니다.")
        self._assert_safe_existing_chain(self.root / "memory")

    @staticmethod
    def _is_reparse_or_link(path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)

    @classmethod
    def _assert_safe_existing_chain(cls, path: Path) -> None:
        absolute = Path(os.path.abspath(str(path)))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            if not current.exists():
                raise KnowledgeError(f"Brain 경로 구성요소가 없습니다: {current}")
            if cls._is_reparse_or_link(current):
                raise KnowledgeError(f"Brain 경로에 symlink/junction/reparse point가 있습니다: {current}")
            if not current.is_dir():
                raise KnowledgeError(f"Brain 경로 구성요소가 디렉터리가 아닙니다: {current}")

    def _ensure_safe_parent(self, parent: Path) -> None:
        lexical_parent = Path(os.path.abspath(str(parent)))
        try:
            lexical_parent.relative_to(self.root)
        except ValueError as error:
            raise KnowledgeError("Brain write parent가 허용된 root 밖입니다.") from error
        current = self.root
        for part in lexical_parent.relative_to(self.root).parts:
            current /= part
            if current.exists():
                if self._is_reparse_or_link(current) or not current.is_dir():
                    raise KnowledgeError(f"Brain write parent가 안전하지 않습니다: {current}")
            else:
                current.mkdir()
                if self._is_reparse_or_link(current) or not current.is_dir():
                    raise KnowledgeError(f"Brain write parent 생성 검증에 실패했습니다: {current}")
        resolved_parent = Path(os.path.realpath(lexical_parent))
        try:
            resolved_parent.relative_to(self._real_root)
        except ValueError as error:
            raise KnowledgeError("Brain write parent의 realpath가 Brain root 밖입니다.") from error

    def _safe_write_once(self, path: Path, content: str) -> bool:
        self._ensure_safe_parent(path.parent)
        # ``Path.exists()`` is false for a dangling symlink on Windows, so
        # inspect lstat/reparse metadata before the existence predicate.
        if self._is_reparse_or_link(path) or (path.exists() and not path.is_file()):
            raise KnowledgeError(f"Brain write target이 안전하지 않습니다: {path}")
        # Re-check immediately before the write-once create/link operation.
        self._ensure_safe_parent(path.parent)
        return self._write_once(path, content)

    @staticmethod
    def _yaml(value: Any) -> str:
        return json.dumps(value if value is not None else "", ensure_ascii=False)

    @staticmethod
    def _clean(value: Any, limit: int) -> str:
        return str(value or "").replace("\x00", "").strip()[:limit]

    def _paths(self, job_id: str) -> tuple[Path, Path]:
        if not _UUID_PATTERN.fullmatch(job_id):
            raise KnowledgeError("job ID가 UUID 형식이 아닙니다.")
        return (
            self.root / "memory" / "ingest" / "url" / f"knowledge-{job_id}.md",
            self.root / "memory" / "ingest" / "insights" / f"knowledge-{job_id}.md",
        )

    @staticmethod
    def _write_once(path: Path, content: str) -> bool:
        return atomic_write_once(path, content)

    def write(self, job: Mapping[str, Any], human_note: str, approved_at: str) -> dict[str, Any]:
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        draft = result.get("draft") if isinstance(result.get("draft"), dict) else None
        if draft is None:
            raise KnowledgeError("검토 후보의 draft가 없거나 손상되었습니다.")
        job_id = str(job.get("id") or "")
        resource_path, insight_path = self._paths(job_id)
        title = self._clean(draft.get("title") or job.get("title"), 300)
        source_url = canonical_url(str(job.get("source_url") or ""))
        citations = [
            self._clean(claim.get("citation"), 100)
            for claim in draft.get("claims", [])
            if isinstance(claim, dict) and claim.get("citation")
        ]
        resource = "\n".join(
            (
                "---",
                "schema_version: knowledge-resource.v1",
                "kind: url",
                "subtype: youtube-notebooklm",
                f"source_url: {self._yaml(source_url)}",
                f"title: {self._yaml(title)}",
                f"job_id: {job_id}",
                f"notebook_id: {self._yaml(job.get('notebook_id'))}",
                f"notebook_source_id: {self._yaml(job.get('notebook_source_id'))}",
                f"source_hash: {self._yaml(job.get('source_hash'))}",
                f"transcript_hash: {self._yaml(job.get('transcript_hash'))}",
                "reviewed_by: yohan",
                f"approved_at: {approved_at}",
                "---",
                "",
                f"# {title}",
                "",
                "## 원본",
                f"- [YouTube 영상 열기]({source_url})",
                f"- 근거 타임스탬프: {', '.join(citations) if citations else '없음'}",
                "",
                "> 원문 자막 전문은 Git에 복제하지 않는다. NotebookLM source와 해시로 추적한다.",
            )
        )
        points = "\n".join(f"- {self._clean(item, 1500)}" for item in draft.get("key_points", []))
        claims = "\n".join(
            f"- **{self._clean(claim.get('type'), 30)}** {self._clean(claim.get('citation'), 100)}: {self._clean(claim.get('statement'), 1500)}"
            for claim in draft.get("claims", [])
            if isinstance(claim, dict)
        )
        uncertainty = "\n".join(f"- {self._clean(item, 1000)}" for item in draft.get("uncertainties", [])) or "- 없음"
        relative_resource = f"memory/ingest/url/{resource_path.name}"
        insight = "\n".join(
            (
                "---",
                f"id: knowledge-{job_id}",
                f"date: {approved_at[:10]}",
                "domain: unclassified",
                "tags: [youtube, notebooklm, knowledge-capture]",
                f"job_id: {job_id}",
                "reviewed_by: yohan",
                f"related: [{relative_resource}]",
                "status: insight",
                "---",
                "",
                f"# {title}",
                "",
                "## 핵심 요약",
                self._clean(draft.get("summary"), 8000),
                "",
                "## 본문 — 논지 전개",
                points or "- 없음",
                "",
                "## 주장·근거 장부",
                claims or "- 없음",
                "",
                "## 내 생각",
                self._clean(human_note, 4000) or "- 없음",
                "",
                "## 인사이트 → 적용",
                self._clean(draft.get("yohan_relevance"), 3000),
                "",
                "## 불확실성",
                uncertainty,
                "",
                "## 출처·원문",
                f"- {relative_resource}",
                f"- {source_url}",
            )
        )
        resource_written = self._safe_write_once(resource_path, resource)
        insight_written = self._safe_write_once(insight_path, insight)
        return {
            "resource_path": relative_resource,
            "insight_path": f"memory/ingest/insights/{insight_path.name}",
            "resource_written": resource_written,
            "insight_written": insight_written,
        }


def _allowlist(env: Mapping[str, str]) -> list[str]:
    raw = env.get("KNOWLEDGE_NOTEBOOK_ALLOWLIST", "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in raw.split(",") if item.strip()]


class KnowledgeService:
    def __init__(
        self,
        notebooklm: NotebookLmClient,
        registry: NotebookRegistry,
        queue: Queue | None = None,
        review_store: ReviewStore | None = None,
        brain_writer_factory: Callable[[], BrainWriter] = BrainWriter,
        caption_provider: CaptionEvidenceProvider | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.notebooklm = notebooklm
        self.registry = registry
        self.queue = queue
        self.review_store = review_store or ReviewStore()
        self.brain_writer_factory = brain_writer_factory
        self.env = env if env is not None else os.environ
        # A provider object alone is not approval.  Every actual transcript
        # fetch needs this explicit operator flag; absent it, the process uses
        # only NotebookLM source_get and source-limited query evidence.
        self.caption_provider = (
            caption_provider
            if self.env.get("KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH", "").strip() == "1"
            else None
        )

    @classmethod
    def from_env(cls, *, require_queue: bool = False) -> "KnowledgeService":
        env = os.environ
        default_notebook_id = env.get("KNOWLEDGE_NOTEBOOK_DEFAULT_ID", "").strip()
        queue = FocusFeedQueue.from_env(env) if require_queue else None
        return cls(
            NotebookLmClient(),
            NotebookRegistry(
                canonical_notebook_ids=[default_notebook_id] if default_notebook_id else (),
            ),
            queue=queue,
            caption_provider=(
                YoutubeCaptionProvider()
                if env.get("KNOWLEDGE_ALLOW_EXTERNAL_TRANSCRIPT_FETCH", "").strip() == "1"
                else None
            ),
            env=env,
        )

    def _queue(self) -> Queue:
        if self.queue is None:
            self.queue = FocusFeedQueue.from_env(self.env)
        return self.queue

    def inventory(self, force: bool = False) -> dict[str, Any]:
        if force or not self.registry.fresh():
            # The index is account-wide. The allowlist controls only where a
            # new source may be added; it must not hide a manual source.
            snapshot = self.registry.refresh(self.notebooklm)
        else:
            snapshot = self.registry.load()
            if snapshot is None:
                snapshot = self.registry.refresh(self.notebooklm)
        duplicates = self.registry.duplicate_groups()
        youtube_sources = [
            source
            for source in snapshot.sources
            if source.source_type.casefold() == "youtube" or source.video_id
        ]
        unresolved_sources = [source for source in snapshot.sources if not source.url]
        unresolved_youtube_sources = [source for source in youtube_sources if not source.url]
        return {
            "ok": True,
            "checked_at": snapshot.checked_at,
            "source_count": len(snapshot.sources),
            "youtube_source_count": len(youtube_sources),
            "unresolved_url_source_count": len(unresolved_sources),
            "unresolved_youtube_source_count": len(unresolved_youtube_sources),
            "manual_preadded_video_id_reuse_complete": not unresolved_youtube_sources,
            "duplicate_groups": duplicates,
            "auto_deleted": False,
        }

    def cleanup_plan(self) -> dict[str, Any]:
        self.inventory(force=False)
        return {
            "ok": True,
            "actions": self.registry.duplicate_groups(),
            "requires_individual_delete_approval": True,
            "mutated": False,
        }

    @staticmethod
    def _worker_id() -> str:
        return f"knowledge-worker:{socket.gethostname()}:{os.getpid()}"

    def process(self, limit: int = MAX_BATCH) -> dict[str, Any]:
        requested = max(1, min(int(limit), MAX_BATCH))
        queue = self._queue()
        self.inventory(force=False)
        jobs = queue.claim(self._worker_id(), requested)
        report: dict[str, Any] = {
            "ok": True,
            "requested_limit": requested,
            "claimed": len(jobs),
            "review_required": [],
            "action_required": [],
            "lease_lost": [],
        }
        for claimed in jobs:
            job = claimed
            source_id: str | None = str(job.get("notebook_source_id") or "") or None
            notebook_id: str | None = str(job.get("notebook_id") or "") or None
            try:
                job = queue.checkpoint(job)
                source_url = str(job.get("source_url") or "")
                # This is an explicit opt-in path only.  Keep its validation
                # ahead of every NotebookLM mutation for the approved legacy
                # workflow; the default path below never enters this branch.
                caption_evidence = (
                    self.caption_provider.fetch(source_url)
                    if self.caption_provider is not None
                    else None
                )
                # Serialize the external check-then-add by canonical source key.
                # An OS lock is released automatically when a worker crashes.
                with self.registry.source_lock(source_url):
                    snapshot = self.registry.refresh(self.notebooklm)
                    matches = self.registry.find(source_url)
                    if bool(source_id) != bool(notebook_id):
                        raise KnowledgeError(
                            "checkpointed NotebookLM identity is incomplete; "
                            "manual source identity confirmation required"
                        )
                    if source_id and notebook_id:
                        checkpointed = next(
                            (
                                source for source in snapshot.sources
                                if source.notebook_id == notebook_id and source.source_id == source_id
                            ),
                            None,
                        )
                        if checkpointed is None:
                            raise KnowledgeError(
                                "checkpointed NotebookLM source is missing; "
                                "manual source identity confirmation required"
                            )
                        if checkpointed.url and checkpointed.key != source_key(source_url):
                            raise KnowledgeError(
                                "checkpointed NotebookLM source conflicts with the queued URL"
                            )
                        if matches and all(
                            match.notebook_id != notebook_id or match.source_id != source_id
                            for match in matches
                        ):
                            raise KnowledgeError(
                                "checkpointed NotebookLM source conflicts with the indexed URL"
                            )
                        notebook_name = checkpointed.notebook_name
                        source_added_at = str(job.get("notebook_source_added_at") or "") or None
                    elif matches:
                        chosen = matches[0]
                        source_id = chosen.source_id
                        notebook_id = chosen.notebook_id
                        notebook_name = chosen.notebook_name
                        source_added_at = None
                    else:
                        unresolved = self.registry.unresolved_youtube_sources()
                        if unresolved:
                            raise KnowledgeError(
                                "manual source identity confirmation required: "
                                f"NotebookLM returned no URL for {len(unresolved)} YouTube source candidate(s)"
                            )
                        title_candidates = self.registry.unresolved_youtube_title_matches(
                            str(job.get("title") or "")
                        )
                        if title_candidates:
                            raise KnowledgeError(
                                "manual source identity confirmation required: "
                                f"NotebookLM returned no URL for {len(title_candidates)} exact-title "
                                "YouTube source candidate(s)"
                            )
                        notebook_id = notebook_id or self.env.get("KNOWLEDGE_NOTEBOOK_DEFAULT_ID", "").strip()
                        if not notebook_id:
                            raise KnowledgeError("기존 source가 없고 KNOWLEDGE_NOTEBOOK_DEFAULT_ID도 없습니다.")
                        allowlist = _allowlist(self.env)
                        if not allowlist:
                            raise KnowledgeError(
                                "KNOWLEDGE_NOTEBOOK_ALLOWLIST must contain an approved notebook before adding a source"
                            )
                        if notebook_id not in allowlist:
                            raise KnowledgeError("default NotebookLM notebook is not in the approved allowlist")
                        try:
                            source_limit = int(self.env.get("KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT", ""))
                        except ValueError:
                            raise KnowledgeError(
                                "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT must be a positive observed account limit"
                            )
                        if source_limit <= 0:
                            raise KnowledgeError(
                                "KNOWLEDGE_NOTEBOOK_SOURCE_LIMIT must be a positive observed account limit"
                            )
                        current_count = sum(
                            1 for source in snapshot.sources
                            if source.notebook_id == notebook_id
                        )
                        rotation_threshold = max(1, int(source_limit * 0.8))
                        if current_count >= rotation_threshold:
                            raise KnowledgeError(
                                f"NotebookLM source limit 80% reached ({current_count}/{source_limit}). "
                                "Notebook rotation approval is required."
                            )
                        source_id = self.notebooklm.add_youtube_source(notebook_id, source_url)
                        notebook_name = notebook_id
                        source_added_at = isoformat(utc_now())
                        self.registry.append(
                            SourceInfo(
                                notebook_id,
                                notebook_name,
                                source_id,
                                str(job.get("title") or source_id),
                                canonical_url(source_url),
                                source_added_at,
                                first_seen_at=source_added_at,
                                status=2,
                                source_type="youtube",
                            )
                        )
                job = queue.checkpoint(
                    job,
                    notebook_id=notebook_id,
                    notebook_name=notebook_name,
                    notebook_source_id=source_id,
                    notebook_source_added_at=source_added_at,
                )
                content = self.notebooklm.get_source(source_id)
                source_hash = sha256_text(content)
                self.review_store.write_source_text(str(job.get("id") or ""), content)
                self.registry.append(
                    SourceInfo(
                        str(notebook_id),
                        str(notebook_name),
                        str(source_id),
                        str(job.get("title") or source_id),
                        canonical_url(str(job.get("source_url") or "")),
                        isoformat(utc_now()),
                        source_hash,
                        first_seen_at=source_added_at or "",
                        status=2,
                        source_type="youtube",
                    )
                )
                # The ordinary path is source_get plus a source-id limited
                # query.  It intentionally makes no secondary HTTP/browser/
                # yt_dlp transcript request.  The legacy caption provider is
                # reachable only through the explicit environment opt-in.
                evidence_contract = NOTEBOOKLM_SOURCE_EVIDENCE_CONTRACT
                if self.caption_provider is not None:
                    assert caption_evidence is not None
                    transcript_hash = caption_evidence.evidence_hash
                    evidence_contract = "caption-v1"
                else:
                    transcript_hash = sha256_text(
                        f"{NOTEBOOKLM_SOURCE_EVIDENCE_CONTRACT}:{source_hash}"
                    )
                job = queue.checkpoint(job, source_hash=source_hash, transcript_hash=transcript_hash)
                prompt = build_query_prompt(job)
                raw = self.notebooklm.query(
                    notebook_id,
                    source_id,
                    prompt,
                )
                draft = parse_draft(raw)
                if draft is None:
                    raw = self.notebooklm.query(
                        notebook_id,
                        source_id,
                        "\n".join(
                            (
                                prompt,
                                "직전 응답 형식이 잘못되었습니다.",
                                "Markdown fence·설명·각주 없이 유효한 JSON 객체 하나만 다시 반환하세요.",
                            )
                        ),
                    )
                    draft = parse_draft(raw)
                if draft is None:
                    queue.complete(
                        job,
                        "action_required",
                        result={"notebook_id": notebook_id, "notebook_source_id": source_id},
                        failure_code="NLM_QUERY_NOT_STRUCTURED",
                        failure_message="NotebookLM 응답이 품질 계약 JSON 형식이 아닙니다.",
                    )
                    report["action_required"].append(str(job.get("id")))
                    continue
                try:
                    draft = (
                        ground_draft_with_caption_evidence(draft, caption_evidence, content)
                        if caption_evidence is not None
                        else ground_draft_with_source_evidence(draft, content, str(source_id))
                    )
                except DraftContractError:
                    # Unknown fields are a persistence-boundary violation, not
                    # a formatting mistake that should be echoed back for repair.
                    raise
                except EvidenceGroundingError:
                    repaired_raw = self.notebooklm.query(
                        notebook_id,
                        source_id,
                        build_grounding_repair_prompt(job, draft),
                    )
                    repaired = parse_draft(repaired_raw)
                    if repaired is None:
                        raise EvidenceGroundingError(
                            "NotebookLM 근거 교정 응답이 JSON 형식이 아닙니다."
                        )
                    draft = (
                        ground_draft_with_caption_evidence(repaired, caption_evidence, content)
                        if caption_evidence is not None
                        else ground_draft_with_source_evidence(repaired, content, str(source_id))
                    )
                quality = evaluate_draft(
                    draft,
                    True,
                    str(job.get("tier") or "T2"),
                    evidence_contract=evidence_contract,
                )
                if not quality["passed"]:
                    queue.complete(
                        job,
                        "action_required",
                        result={"notebook_id": notebook_id, "notebook_source_id": source_id, "draft": draft},
                        quality_score=quality["score"],
                        quality_report=quality,
                        failure_code="QUALITY_GATE_FAILED",
                        failure_message=" ".join(quality["hard_failures"]) or f"품질 점수 {quality['score']}/100",
                    )
                    report["action_required"].append(str(job.get("id")))
                    continue
                review = {
                    "job_id": job.get("id"),
                    "source_url": canonical_url(str(job.get("source_url") or "")),
                    "notebook_id": notebook_id,
                    "notebook_source_id": source_id,
                    "source_hash": source_hash,
                    "transcript_hash": transcript_hash,
                    "quality": quality,
                    "draft": draft,
                }
                path = self.review_store.write(str(job.get("id") or ""), review)
                queue.complete(
                    job,
                    "review_required",
                    result={**review, "review_path": str(path)},
                    quality_score=quality["score"],
                    quality_report=quality,
                )
                report["review_required"].append(str(job.get("id")))
            except LeaseLostError:
                report["lease_lost"].append(str(job.get("id")))
            except Exception as error:  # boundary: every claimed row must leave a clear state
                try:
                    failure_code = processing_failure_code(error)
                    queue.complete(
                        job,
                        "action_required",
                        result={"notebook_id": notebook_id, "notebook_source_id": source_id},
                        failure_code=failure_code,
                        failure_message=safe_error(error),
                    )
                    report["action_required"].append(str(job.get("id")))
                except LeaseLostError:
                    report["lease_lost"].append(str(job.get("id")))
        return report

    def reviews(self) -> dict[str, Any]:
        items = []
        for job in self._queue().reviews():
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            if result.get("review_decision") == "deferred":
                continue
            draft = result.get("draft") if isinstance(result.get("draft"), dict) else {}
            quality = job.get("quality_report") if isinstance(job.get("quality_report"), dict) else {}
            quality_warnings = [
                str(item)
                for item in [
                    *(quality.get("hard_failures") if isinstance(quality.get("hard_failures"), list) else []),
                    *(quality.get("warnings") if isinstance(quality.get("warnings"), list) else []),
                ]
                if str(item).strip()
            ]
            recovering_approval = job.get("status") == "approving"
            if recovering_approval:
                quality_warnings.append("승인 적재 복구가 필요합니다. 같은 승인 intent로 재시도하세요.")
            approval_blockers: list[str] = []
            try:
                self._validate_approval_contract(job)
            except KnowledgeError as error:
                approval_blockers.append(safe_error(error, 300))
            attempt_count = int(job.get("attempt_count") or 0)
            missing_timestamps = self._missing_public_caption_timestamps(job)
            reprocess_blockers: list[str] = []
            if job.get("status") != "review_required":
                reprocess_blockers.append("승인 적재 복구 중인 항목은 재처리로 전환할 수 없습니다.")
            metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
            legacy_recovery_used = metadata.get("_legacy_review_recovery_v1") is True
            if attempt_count > 3 or legacy_recovery_used:
                reprocess_blockers.append("레거시 검토의 1회 복구 한도에 도달했습니다.")
            if not missing_timestamps:
                reprocess_blockers.append("공개 자막 타임스탬프 누락 유형이 아닙니다.")
            preserved_fields = ("notebook_id", "notebook_source_id", "source_hash", "transcript_hash")
            if any(not isinstance(job.get(field), str) or not str(job.get(field)).strip() for field in preserved_fields):
                reprocess_blockers.append("보존할 NotebookLM source/hash 식별자가 없습니다.")
            elif any(
                not re.fullmatch(r"[0-9a-f]{64}", str(job[field]), re.IGNORECASE)
                for field in ("source_hash", "transcript_hash")
            ):
                reprocess_blockers.append("보존할 source/hash 식별자가 SHA-256 형식이 아닙니다.")
            items.append(
                {
                    "jobId": job.get("id"),
                    "title": job.get("title") or f"YouTube {job.get('video_id') or ''}".strip(),
                    "sourceType": job.get("source_type"),
                    "sourceUrl": job.get("source_url"),
                    "notebookId": job.get("notebook_id"),
                    "notebookSourceId": job.get("notebook_source_id"),
                    "qualityScore": job.get("quality_score"),
                    "qualityWarnings": quality_warnings,
                    "summary": draft.get("summary"),
                    "keyPoints": draft.get("key_points", []),
                    "claims": draft.get("claims", []),
                    "uncertainties": draft.get("uncertainties", []),
                    "category": result.get("category", "YT · 미분류 · Inbox"),
                    "status": "review_required" if recovering_approval else job.get("status"),
                    "approvalRecovery": recovering_approval,
                    "approvalReady": not approval_blockers,
                    "approvalBlockers": approval_blockers,
                    "reprocessEligible": not reprocess_blockers,
                    "reprocessBlockers": reprocess_blockers,
                    "attemptCount": attempt_count,
                }
            )
        return {"ok": True, "items": items}

    def retry(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        job = self._queue().retry(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "status": job.get("status"),
            "attempt_count": job.get("attempt_count"),
            "notebook_id": job.get("notebook_id"),
            "notebook_source_id": job.get("notebook_source_id"),
        }

    @staticmethod
    def _missing_public_caption_timestamps(job: Mapping[str, Any]) -> bool:
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        draft = result.get("draft") if isinstance(result.get("draft"), dict) else {}
        coverage = draft.get("coverage") if isinstance(draft.get("coverage"), dict) else {}
        for part in ("start", "middle", "end"):
            evidence = coverage.get(part) if isinstance(coverage.get(part), dict) else {}
            if not _TIMESTAMP_PATTERN.search(str(evidence.get("citation") or "")):
                return True
        claims = draft.get("claims") if isinstance(draft.get("claims"), list) else []
        if not claims:
            return True
        return any(
            not isinstance(claim, dict)
            or not _TIMESTAMP_PATTERN.search(str(claim.get("citation") or ""))
            for claim in claims
        )

    def invalidate_review(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        queue = self._queue()
        job = queue.get(job_id)
        if job.get("status") != "review_required":
            raise KnowledgeError(f"검토 대기 작업만 무효화할 수 있습니다: {job.get('status')}")
        attempt_count = int(job.get("attempt_count") or 0)
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        legacy_recovery_used = metadata.get("_legacy_review_recovery_v1") is True
        if attempt_count > 3 or legacy_recovery_used:
            raise KnowledgeError("레거시 검토의 1회 복구 한도에 도달했습니다.")
        if not self._missing_public_caption_timestamps(job):
            raise KnowledgeError("공개 자막 타임스탬프가 완전한 검토는 무효화할 수 없습니다.")
        preserved_fields = ("notebook_id", "notebook_source_id", "source_hash", "transcript_hash")
        if any(not isinstance(job.get(field), str) or not str(job.get(field)).strip() for field in preserved_fields):
            raise KnowledgeError("안전한 재처리에 필요한 NotebookLM source/hash 식별자가 없습니다.")
        for field in ("source_hash", "transcript_hash"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(job[field]), re.IGNORECASE):
                raise KnowledgeError(f"안전한 재처리에 필요한 {field}가 SHA-256 해시가 아닙니다.")
        invalidated = queue.invalidate_review(job_id)
        if invalidated.get("status") != "action_required" or invalidated.get("failure_code") != "PUBLIC_CAPTION_TIMESTAMPS_REQUIRED":
            raise KnowledgeError("검토 무효화 응답의 상태 계약이 올바르지 않습니다.")
        if any(invalidated.get(field) != job.get(field) for field in preserved_fields):
            raise KnowledgeError("검토 무효화 중 NotebookLM source/hash 식별자가 변경되었습니다.")
        return {
            "ok": True,
            "job_id": job_id,
            "status": invalidated.get("status"),
            "failure_code": invalidated.get("failure_code"),
            "attempt_count": invalidated.get("attempt_count"),
            "notebook_id": invalidated.get("notebook_id"),
            "notebook_source_id": invalidated.get("notebook_source_id"),
        }

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not _UUID_PATTERN.fullmatch(job_id):
            raise KnowledgeError("job ID가 UUID 형식이 아닙니다.")

    @staticmethod
    def _validate_approval_contract(job: Mapping[str, Any]) -> None:
        """Reject incomplete review rows before creating an approval intent.

        The database row is untrusted input at this boundary.  In particular,
        a high score alone must not turn an ungrounded or partially checkpointed
        row into a Brain write.
        """
        required = ("id", "notebook_id", "notebook_source_id", "source_hash", "transcript_hash")
        if any(not isinstance(job.get(name), str) or not str(job.get(name)).strip() for name in required):
            raise KnowledgeError("승인 계약에 필수 job/notebook/source/hash 필드가 없습니다.")
        for name in ("source_hash", "transcript_hash"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(job[name]), re.IGNORECASE):
                raise KnowledgeError(f"승인 계약의 {name}가 SHA-256 해시가 아닙니다.")
        try:
            canonical_url(str(job.get("source_url") or ""))
        except KnowledgeError as error:
            raise KnowledgeError("승인 계약에 유효한 source_url이 없습니다.") from error
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        draft = result.get("draft") if isinstance(result.get("draft"), dict) else None
        if draft is None:
            raise KnowledgeError("승인 계약에 검증할 draft가 없습니다.")
        _sanitize_draft_contract(draft)
        quality = job.get("quality_report") if isinstance(job.get("quality_report"), dict) else {}
        if quality.get("passed") is not True:
            raise KnowledgeError("승인 계약의 quality_report.passed가 true가 아닙니다.")
        if int(quality.get("score") or 0) < QUALITY_THRESHOLD:
            raise KnowledgeError("승인 계약의 quality_report 점수가 부족합니다.")
        evidence_contract = quality.get("evidence_contract")
        if evidence_contract not in {"caption-v1", NOTEBOOKLM_SOURCE_EVIDENCE_CONTRACT}:
            raise KnowledgeError("승인 계약의 근거 방식이 검증되지 않았습니다.")
        for part in ("start", "middle", "end"):
            evidence = draft.get("coverage", {}).get(part) if isinstance(draft.get("coverage"), dict) else None
            if not isinstance(evidence, dict) or evidence.get("citation_verified") is not True or not str(evidence.get("evidence_quote") or "").strip():
                raise KnowledgeError("승인 계약의 coverage 근거가 불완전합니다.")
            if not _TIMESTAMP_PATTERN.search(str(evidence.get("citation") or "")):
                raise KnowledgeError("승인 계약에는 시작·중간·끝 timestamp 근거가 필요합니다.")
        for claim in draft.get("claims", []):
            if isinstance(claim, dict) and claim.get("type") == "fact" and (
                claim.get("citation_verified") is not True
                or not str(claim.get("evidence_quote") or "").strip()
                or not _TIMESTAMP_PATTERN.search(str(claim.get("citation") or ""))
            ):
                raise KnowledgeError("승인 계약의 사실 주장 근거가 불완전합니다.")
        if str(job.get("tier") or "T2") == "T3":
            second = quality.get("second_evaluator")
            if not isinstance(second, dict) or second.get("passed") is not True:
                raise KnowledgeError("T3 승인은 독립 second_evaluator 통과가 필요합니다.")

    def approve(self, job_id: str, human_note: str = "") -> dict[str, Any]:
        self._validate_job_id(job_id)
        if len(human_note.strip()) > 4000:
            raise KnowledgeError("humanNote는 4,000자 이하여야 합니다.")
        queue = self._queue()
        job = queue.get(job_id)
        if job.get("status") == "completed":
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            return {"ok": True, "idempotent": True, "job_id": job_id, **result}
        if job.get("status") not in {"review_required", "approving"}:
            raise KnowledgeError(f"검토 대기 작업만 승인할 수 있습니다: {job.get('status')}")
        quality_score = int(job.get("quality_score") or 0)
        if quality_score < QUALITY_THRESHOLD:
            raise KnowledgeError(f"품질 점수 {quality_score}는 승인 기준 {QUALITY_THRESHOLD} 미만입니다.")
        self._validate_approval_contract(job)
        requested_note = human_note.strip()
        local_intent = self.review_store.read_approval_intent(job_id)
        if local_intent is not None:
            effective_note = local_intent["human_note"]
            intent_hash = local_intent["intent_hash"]
            if requested_note and requested_note != effective_note:
                raise KnowledgeError("진행 중인 승인과 다른 메모로 재시도할 수 없습니다.")
        else:
            if job.get("status") == "approving":
                raise KnowledgeError("진행 중인 승인의 로컬 write-once intent가 없습니다.")
            effective_note = requested_note
            intent_hash = sha256_text(effective_note)
            self.review_store.write_approval_intent(job_id, intent_hash, effective_note)
        if job.get("status") == "approving" and job.get("approval_intent_hash") != intent_hash:
            raise KnowledgeError("DB 승인 hash와 로컬 approval intent가 다릅니다.")
        job = queue.begin_approval(job, intent_hash)
        if job.get("status") == "completed":
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            return {"ok": True, "idempotent": True, "job_id": job_id, **result}
        approval_token = str(job.get("approval_token") or "")
        approved_at = str(job.get("approval_started_at") or "")
        persisted_hash = str(job.get("approval_intent_hash") or "")
        if (
            not _UUID_PATTERN.fullmatch(approval_token)
            or not approved_at
            or persisted_hash != intent_hash
        ):
            raise KnowledgeError("승인 CAS 응답이 불완전하거나 요청 intent와 다릅니다.")
        written = self.brain_writer_factory().write(job, effective_note, approved_at)
        completed_result = {
            **(job.get("result") if isinstance(job.get("result"), dict) else {}),
            **written,
            "approved_at": approved_at,
            "approval_intent_hash": intent_hash,
            "promotion_pending": True,
        }
        queue.mark_completed(job, completed_result)
        return {"ok": True, "idempotent": False, "job_id": job_id, **written}

    def defer(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        job = self._queue().get(job_id)
        if job.get("status") != "review_required":
            raise KnowledgeError(f"검토 대기 작업만 보류할 수 있습니다: {job.get('status')}")
        self._queue().defer(job)
        return {"ok": True, "job_id": job_id, "decision": "deferred"}

    def reject(self, job_id: str) -> dict[str, Any]:
        self._validate_job_id(job_id)
        job = self._queue().get(job_id)
        if job.get("status") != "review_required":
            raise KnowledgeError(f"검토 대기 작업만 거절할 수 있습니다: {job.get('status')}")
        self._queue().reject(job)
        return {"ok": True, "job_id": job_id, "decision": "rejected"}

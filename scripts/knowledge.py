# -*- coding: utf-8 -*-
"""CLI entrypoint for the Focus Feed knowledge workflow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 로컬 CLI와 Control Tower가 같은 yohan-mcp 설정을 사용한다. 이미 주입된
# 프로세스 환경은 덮어쓰지 않아 테스트/명시적 운영 설정의 우선순위를 보존한다.
load_dotenv(ROOT / ".env", override=False)

from core.knowledge import KnowledgeError, KnowledgeService, safe_error  # noqa: E402


def configure_utf8_stdio(*streams: Any) -> None:
    """Keep machine JSON valid when Windows pipes default to a legacy code page."""
    targets = streams or (sys.stdout, sys.stderr)
    for stream in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _stdin_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if len(raw.encode("utf-8")) > 16_384:
        raise KnowledgeError("stdin 요청이 너무 큽니다.")
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise KnowledgeError("stdin은 JSON 객체여야 합니다.") from error
    if not isinstance(value, dict):
        raise KnowledgeError("stdin은 JSON 객체여야 합니다.")
    extra = set(value) - {"humanNote"}
    if extra:
        raise KnowledgeError(f"허용되지 않은 stdin 필드입니다: {', '.join(sorted(extra))}")
    note = value.get("humanNote", "")
    if not isinstance(note, str):
        raise KnowledgeError("humanNote는 문자열이어야 합니다.")
    if len(note.strip()) > 4_000:
        raise KnowledgeError("humanNote는 4,000자 이하여야 합니다.")
    return {"humanNote": note.strip()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowledge", description="Focus Feed 지식 대기열 처리")
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", help="NotebookLM 전체 source 인덱스 갱신")
    inventory.add_argument("--force", action="store_true", help="24시간 캐시를 무시하고 다시 읽기")

    commands.add_parser("cleanup-plan", help="중복 source 정리 후보만 출력")

    process = commands.add_parser("process", help="대기열을 최대 3건 처리")
    process.add_argument("--limit", type=int)
    process.add_argument("--job-id")
    process.add_argument("--expected-git-sha")

    doctor = commands.add_parser("doctor", help="exact-job execution read-only preflight")
    doctor.add_argument("--job-id", required=True)

    canary = commands.add_parser("canary", help="clean canary inspection helpers")
    canary_commands = canary.add_subparsers(dest="canary_command", required=True)
    inspect = canary_commands.add_parser("inspect", help="inspect jobs created for a canary manifest")
    inspect.add_argument("--manifest", required=True)

    commands.add_parser("reviews", help="사람 검토 대기 목록 출력")

    retry = commands.add_parser("retry", help="조치 완료 후 허용된 작업 한 건을 다시 대기열에 넣기")
    retry.add_argument("job_id")

    invalidate_review = commands.add_parser(
        "invalidate-review",
        help="타임스탬프가 불완전한 레거시 검토를 조치 필요 상태로 격리",
    )
    invalidate_review.add_argument("job_id")

    approve = commands.add_parser("approve", help="검토 항목을 brain에 write-once 승인 적재")
    approve.add_argument("job_id")
    approve.add_argument("--stdin", action="store_true", help="{humanNote} JSON을 stdin으로 받기")

    defer = commands.add_parser("defer", help="검토 항목 보류")
    defer.add_argument("job_id")

    reject = commands.add_parser("reject", help="검토 항목 거절")
    reject.add_argument("job_id")

    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    needs_queue = args.command in {
        "process",
        "doctor",
        "canary",
        "reviews",
        "retry",
        "invalidate-review",
        "approve",
        "defer",
        "reject",
    }
    service = KnowledgeService.from_env(require_queue=needs_queue)
    if args.command == "inventory":
        return service.inventory(force=args.force)
    if args.command == "cleanup-plan":
        return service.cleanup_plan()
    if args.command == "process":
        if args.job_id and args.limit is not None:
            raise KnowledgeError("--job-id and --limit cannot be used together")
        if args.job_id:
            if not args.expected_git_sha:
                raise KnowledgeError("--job-id requires --expected-git-sha")
            return service.process(
                limit=1,
                job_id=args.job_id,
                expected_git_sha=args.expected_git_sha,
            )
        if args.expected_git_sha:
            raise KnowledgeError("--expected-git-sha requires --job-id")
        return service.process(limit=args.limit if args.limit is not None else 3)
    if args.command == "doctor":
        return service.doctor(args.job_id)
    if args.command == "canary" and args.canary_command == "inspect":
        return service.canary_inspect(Path(args.manifest).expanduser().resolve())
    if args.command == "reviews":
        return service.reviews()
    if args.command == "retry":
        return service.retry(args.job_id)
    if args.command == "invalidate-review":
        return service.invalidate_review(args.job_id)
    if args.command == "approve":
        payload = _stdin_payload() if args.stdin else {"humanNote": ""}
        return service.approve(args.job_id, payload["humanNote"])
    if args.command == "defer":
        return service.defer(args.job_id)
    if args.command == "reject":
        return service.reject(args.job_id)
    raise KnowledgeError(f"지원하지 않는 명령입니다: {args.command}")


def main() -> int:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as error:
        print(json.dumps({"ok": False, "error": safe_error(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok", False) else 2


if __name__ == "__main__":
    raise SystemExit(main())

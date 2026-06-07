#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yohan-mcp v2 — P1 스키마/관계 검증기.

검사 항목:
  1) schemas/ 아래 모든 *.schema.json 이 draft 2020-12 메타스키마로서 유효한가
  2) 각 스키마에 title·description 이 있고, 최상위 property 마다 description·examples 가 있는가
  3) _shared-enums.json 의 모든 $defs 가 title·description·examples 를 갖는가
  4) _links.json 의 source/target 노드가 실재 스키마(또는 허용된 외부 백엔드)를 가리키는가

사용법:
  python scripts/validate_schemas.py

종료 코드: 통과 0 / 실패 1
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

# Windows 콘솔(cp949) 에서도 한글/이모지 출력이 깨지지 않도록 UTF-8 강제
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 스키마 파일이 없어도 되는 외부 백엔드(P1 에선 스키마를 두지 않음)
EXTERNAL_BACKENDS = {"qdrant", "n8n"}
# 스키마 파일이 존재해야 하는 내부 백엔드 → schemas/<backend>/<entity>.schema.json
INTERNAL_BACKENDS = {"notion", "memory", "studio"}

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "schemas"


class Reporter:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.checked = 0

    def fail(self, msg: str) -> None:
        self.errors.append(msg)

    def ok(self) -> bool:
        return not self.errors


def iter_schema_files() -> list[Path]:
    return sorted(SCHEMAS_DIR.rglob("*.schema.json"))


def load_json(path: Path, rep: Reporter):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        rep.fail(f"[JSON] {path.relative_to(ROOT)} 파싱 실패: {exc}")
        return None


def check_schema_doc(path: Path, schema: dict, rep: Reporter) -> None:
    rel = path.relative_to(ROOT)
    # (1) 메타스키마 유효성
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        rep.fail(f"[META] {rel}: 유효하지 않은 스키마 — {exc.message}")
        return
    # (2) title·description
    for key in ("title", "description"):
        if not schema.get(key):
            rep.fail(f"[DOC] {rel}: 최상위 '{key}' 누락")
    # (2) 최상위 property 마다 description·examples
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        has_ref = "$ref" in prop  # enum 참조 필드는 _shared-enums 에서 문서화됨
        if not prop.get("description"):
            rep.fail(f"[DOC] {rel}: property '{name}' description 누락")
        if not has_ref and "examples" not in prop:
            rep.fail(f"[DOC] {rel}: property '{name}' examples 누락")
    rep.checked += 1


def check_shared_enums(rep: Reporter) -> None:
    path = SCHEMAS_DIR / "_shared-enums.json"
    if not path.exists():
        rep.fail("[ENUM] _shared-enums.json 없음")
        return
    data = load_json(path, rep)
    if data is None:
        return
    for name, d in (data.get("$defs") or {}).items():
        for key in ("title", "description", "examples"):
            if key not in d or not d.get(key) and key != "examples":
                rep.fail(f"[ENUM] $defs/{name}: '{key}' 누락")
            if key == "examples" and "examples" not in d:
                rep.fail(f"[ENUM] $defs/{name}: 'examples' 누락")


def node_to_path(node: str, rep: Reporter) -> None:
    try:
        backend, entity = node.split(":", 1)
    except ValueError:
        rep.fail(f"[LINK] 노드 형식 오류 '{node}' (기대: backend:entity)")
        return
    if backend in EXTERNAL_BACKENDS:
        return  # 외부 백엔드 — 스키마 파일 없음(정상)
    if backend not in INTERNAL_BACKENDS:
        rep.fail(f"[LINK] 알 수 없는 backend '{backend}' (노드 '{node}')")
        return
    if entity == "*":
        return  # 와일드카드 허용
    target = SCHEMAS_DIR / backend / f"{entity}.schema.json"
    if not target.exists():
        rep.fail(f"[LINK] 노드 '{node}' → 스키마 파일 없음: {target.relative_to(ROOT)}")


def check_links(rep: Reporter) -> None:
    path = SCHEMAS_DIR / "_links.json"
    if not path.exists():
        rep.fail("[LINK] _links.json 없음")
        return
    data = load_json(path, rep)
    if data is None:
        return
    links = data.get("links") or []
    if not links:
        rep.fail("[LINK] links 배열이 비어있음")
    for i, link in enumerate(links):
        for field in ("source", "target", "relation", "cardinality", "description"):
            if not link.get(field):
                rep.fail(f"[LINK] links[{i}]: '{field}' 누락")
        if link.get("source"):
            node_to_path(link["source"], rep)
        if link.get("target"):
            node_to_path(link["target"], rep)


def main() -> int:
    rep = Reporter()
    files = iter_schema_files()
    if not files:
        rep.fail("[META] schemas/ 아래 *.schema.json 파일을 찾지 못함")
    for path in files:
        schema = load_json(path, rep)
        if schema is not None:
            check_schema_doc(path, schema, rep)
    check_shared_enums(rep)
    check_links(rep)

    print(f"검사한 스키마 문서: {rep.checked}개 / 발견된 파일: {len(files)}개")
    if rep.ok():
        print("✅ 전체 통과 — 모든 스키마 유효, 문서/예시 완비, 링크 노드 일치")
        return 0
    print(f"❌ 실패 — 문제 {len(rep.errors)}건:")
    for e in rep.errors:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

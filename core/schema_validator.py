# -*- coding: utf-8 -*-
"""yohan-mcp v2 — P1 schemas/ 로더 + 입출력 검증 (P2).

- `schemas/` 의 모든 *.schema.json 을 로드해 "<backend>:<type>" 와 짧은 "<type>" 키로 등록.
- `_shared-enums.json` 을 referencing Registry 에 등록해 상대 $ref 를 해소.
- `validate(type_, data)` 로 도구 입출력을 P1 스키마에 맞춰 검증.
- `backend_of(type_)` 로 타입 → 백엔드 자동 선택(create/update 라우팅에 사용).
"""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "schemas"


class SchemaValidator:
    def __init__(self, schemas_dir: Path | None = None) -> None:
        self.dir = Path(schemas_dir) if schemas_dir else SCHEMAS_DIR
        self._schemas: dict[str, dict] = {}        # "backend:type" -> schema
        self._short: dict[str, str] = {}           # "type" -> "backend:type" (유일할 때만)
        self._backend: dict[str, str] = {}         # "backend:type" -> backend
        self._registry: Registry = Registry()
        self._load()

    def _load(self) -> None:
        # 공유 enum 을 $id 기준으로 registry 등록 → 상대 $ref 해소
        shared_path = self.dir / "_shared-enums.json"
        if shared_path.exists():
            shared = json.loads(shared_path.read_text(encoding="utf-8"))
            self._registry = self._registry.with_resource(
                uri=shared["$id"], resource=Resource.from_contents(shared)
            )
        # 각 백엔드 스키마 로드
        short_seen: dict[str, int] = {}
        for path in sorted(self.dir.rglob("*.schema.json")):
            backend = path.parent.name           # notion / memory / studio
            type_ = path.stem.replace(".schema", "")
            schema = json.loads(path.read_text(encoding="utf-8"))
            key = f"{backend}:{type_}"
            self._schemas[key] = schema
            self._backend[key] = backend
            short_seen[type_] = short_seen.get(type_, 0) + 1
            self._short[type_] = key
            # registry 에도 등록(스키마 간 상호참조 대비)
            if "$id" in schema:
                self._registry = self._registry.with_resource(
                    uri=schema["$id"], resource=Resource.from_contents(schema)
                )
        # 짧은 키가 중복되면 제거(모호성 방지 — 명시적 backend:type 만 허용)
        for type_, n in short_seen.items():
            if n > 1:
                self._short.pop(type_, None)

    def _resolve_key(self, type_: str) -> str | None:
        if type_ in self._schemas:
            return type_
        if type_ in self._short:
            return self._short[type_]
        return None

    def known_types(self) -> list[str]:
        return sorted(self._schemas.keys())

    def backend_of(self, type_: str) -> str | None:
        """타입 → 백엔드 이름. create/update 자동 라우팅에 사용."""
        key = self._resolve_key(type_)
        return self._backend.get(key) if key else None

    def validate(self, type_: str, data: dict) -> tuple[bool, list[str]]:
        """data 를 type_ 스키마로 검증. (valid, errors) 반환."""
        key = self._resolve_key(type_)
        if key is None:
            return False, [f"알 수 없는 타입: {type_}"]
        return self._run(self._schemas[key], data)

    def validate_partial(self, type_: str, data: dict) -> tuple[bool, list[str]]:
        """update 용 부분 검증 — required 를 제거해 제공된 필드의 타입만 검사."""
        key = self._resolve_key(type_)
        if key is None:
            return False, [f"알 수 없는 타입: {type_}"]
        schema = dict(self._schemas[key])
        schema.pop("required", None)
        return self._run(schema, data)

    def _run(self, schema: dict, data: dict) -> tuple[bool, list[str]]:
        # format_checker 로 date-time·uri 등 format 까지 실제 검증
        # (rfc3339-validator / rfc3986-validator 설치 시 활성)
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FormatChecker()
        )
        errors = [
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        ]
        return (len(errors) == 0), errors

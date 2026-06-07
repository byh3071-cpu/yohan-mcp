# Windows 콘솔 UTF-8 강제 (cp949 UnicodeEncodeError)

- **패턴명**: Windows 콘솔 UTF-8 강제
- **카테고리**: env
- **발견일**: 2026-06-08
- **출처 프로젝트**: yohan-mcp v2 (P1·P2)
- **태그**: windows, cp949, unicode, stdout, python, encoding

## 증상

Python 스크립트가 한글/이모지를 `print` 하면 Windows 기본 콘솔(코드페이지 949)에서 크래시:

```
UnicodeEncodeError: 'cp949' codec can't encode character '✅' in position 0: illegal multibyte sequence
```

- ✅/❌ 같은 이모지, 또는 한글 출력 시 발생.
- 검증 스크립트·CLI·MCP 서버 등 **진입점 .py** 에서 자주 터진다.
- 로직은 정상인데 마지막 `print` 한 줄에서만 죽어 원인 오인하기 쉬움.

## 원인

Windows 의 `sys.stdout` 은 콘솔 기본 인코딩(보통 cp949)으로 텍스트를 인코딩한다.
cp949 에 없는 문자(이모지, 일부 유니코드)를 만나면 인코딩 실패 → `UnicodeEncodeError`.
PowerShell/cmd 의 활성 코드페이지가 65001(UTF-8)이 아니면 재현된다.

## 해결

진입점 최상단에서 표준 스트림을 UTF-8 로 재구성한다 (Python 3.7+):

```python
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
```

- `hasattr` 가드: 재구성 불가한 스트림(파이프 리다이렉트 등)에서 안전.
- `reconfigure` 는 스트림 객체를 바꾸지 않고 코덱만 교체 → 버퍼링/식별자 유지.

### 주의 — stdio 프로토콜 서버는 stdout 건드리지 말 것

MCP(stdio)·LSP 처럼 **stdout 으로 JSON-RPC 를 주고받는** 프로세스는 stdout 을
재구성하지 말고 **stderr 만** 재구성한다. SDK 가 stdout 에 자체 UTF-8 래퍼
(`TextIOWrapper(sys.stdout.buffer, encoding="utf-8")`)를 씌우므로, 진단/예외는
stderr 로 보낸다. 런타임 경로에서 `sys.stdout` 직접 `print` 금지(프레이밍 오염).

```python
# stdio 서버 진입점
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
```

## 적용 조건

- OS: Windows (cp949/비-UTF-8 콘솔)
- 모든 Python **진입점**(`if __name__ == "__main__"` 가진 스크립트, CLI, 서버)
- 한글/이모지/비-ASCII 를 stdout/stderr 로 출력하는 경우
- 대안: 환경변수 `PYTHONUTF8=1` 또는 `chcp 65001` 도 가능하나, 코드 내 `reconfigure`
  가 실행 환경에 의존하지 않아 가장 견고.
- 영구 설정(시스템 전역, 새 콘솔/프로세스부터 적용 — VSCode 등 실행 중 프로세스엔 소급 안 됨):

  ```powershell
  [Environment]::SetEnvironmentVariable("PYTHONUTF8","1","User")  # 새 창부터 적용
  ```

## 출처 DevLog

yohan-mcp P1(`scripts/validate_schemas.py` 성공 출력 크래시) → P2(`server.py` stdio 서버에서
stdout 재구성의 위험성까지 확장). 두 케이스 모두 이 패턴으로 해소.

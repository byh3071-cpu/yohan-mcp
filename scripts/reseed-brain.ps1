# 브레인 벡터 재시딩 — Claude Code(=yohan MCP 서버)를 종료한 상태에서 실행할 것.
#
# 왜 종료가 필요한가: 로컬 파일 모드 Qdrant 는 스토리지 디렉터리에 단일 프로세스 파일 락을
# 건다. MCP 서버가 떠 있으면 그 락을 쥐고 있어서 이 스크립트는 PermissionError(Errno 13)로
# 즉시 죽는다. 서버 모드(URL)였다면 동시 접근이 되지만, Docker 없이 가려고 파일 모드를
# 택했으므로 이 제약은 설계상 감수한 대가다.
#
# 실행:  powershell -NoProfile -File "C:\Users\Public\dev\yohan-ecosystem\yohan-mcp\scripts\reseed-brain.ps1"

$ErrorActionPreference = 'Stop'

$root = 'C:\Users\Public\dev\yohan-ecosystem'
$py   = Join-Path $root 'yohan-mcp\.venv\Scripts\python.exe'
$seed = Join-Path $root 'yohan-mcp\scripts\seed_brain_memory.py'

# .env 에 의존하지 않고 여기서 못박는다 — 사용자 환경변수는 CC 를 띄운 셸에만 상속되고
# 이 스크립트에는 안 닿을 수 있다(실제로 그 문제로 브레인 경로가 어긋났던 적이 있다).
$env:YOHAN_BRAIN_ROOT = Join-Path $root 'yohan-brain'
$env:QDRANT_PATH      = Join-Path $root 'yohan-mcp\.qdrant_storage'
$env:EMBED_BACKEND    = 'ollama'
$env:EMBEDDING_MODEL  = 'bge-m3'
$env:OLLAMA_TIMEOUT   = '300'   # 전량 재시딩 시 첫 배치가 느려 기본값으론 끊긴다

foreach ($p in @($py, $seed)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "경로 없음: $p" }
}

Write-Host "브레인 재시딩 시작 — 증분(변경/신규 파일만)" -ForegroundColor Cyan
Write-Host "  brain : $env:YOHAN_BRAIN_ROOT"
Write-Host "  qdrant: $env:QDRANT_PATH`n"

& $py $seed
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "완료 — Claude Code 를 다시 켜면 확장된 색인이 붙는다." -ForegroundColor Green
} else {
    # 락 충돌이 압도적으로 흔한 실패라 원인을 콕 집어준다.
    Write-Host "실패 (exit $code)" -ForegroundColor Red
    Write-Host "PermissionError/Errno 13 이면 Claude Code 가 아직 떠 있다는 뜻 — 완전히 종료 후 재실행." -ForegroundColor Yellow
}
exit $code

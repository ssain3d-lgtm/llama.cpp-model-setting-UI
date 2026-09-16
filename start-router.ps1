# ============================================================
#  llama.cpp 라우터 서버  -  LM Studio 의 모델/MCP 를 그대로 공유
#  - WebUI / API : http://127.0.0.1:8080   (API 는 OpenAI 호환, "model" = LM Studio 와 같은 식별자)
#  - 시작할 때마다 sync-lmstudio.py 가 LM Studio 폴더를 스캔해 models.ini / mcp.json / ui-config.json 을 다시 만듦
#      models.ini  : LM Studio 모델 전부 + 모델별 로드 설정(ctx, KV 캐시 등) 그대로
#      mcp.json    : ~/.lmstudio/mcp.json 의 MCP 서버 전부
#      손보고 싶으면 models.override.ini / mcp.override.json 편집 (자동 생성 파일은 직접 고치지 말 것)
#  - 한 번에 1개 모델만 VRAM (-ModelsMax 로 변경). 다른 모델 요청 시 자동 교체.
#  - 요청이 없으면 LM Studio JIT TTL(기본 3600초)과 동일하게 VRAM 해제, 다음 요청 때 자동 재로드
#  - 실행 중에도 watch-lmstudio.py 가 LM Studio 모델 폴더를 감시 (15초 간격, log: lmstudio-watch.log)
#      새로 받은/지운 모델, LM Studio 모델별 설정, models.override.ini 변경 -> models.ini 재생성 + 라우터 reload (재시작 불필요)
#      실행 중인 모델은 자기 설정이 바뀐 경우에만 언로드됨. 끄려면 -NoWatch
#
#  MCP 모드 (-McpMode)
#    browser (기본)  MCP 서버들을 mcp-http-gateway.py(포트 8091)로 HTTP 노출하고 WebUI 의 "MCP Servers" 에 등록
#                    -> LM Studio 처럼 서버 단위로 켜고 끄기 / 대화별 선택. 켠 서버의 툴만 프롬프트에 들어감 (토큰 절약)
#                    -> 서버 프로세스는 쓸 때만 기동, 10분 유휴면 종료
#    server          llama-server 가 직접 MCP 서버 18개를 전부 띄움 (툴 197개 항상 포함, 약 108K 토큰/요청)
#    off             MCP 없음
#
#    .\start-router.ps1                       기본 (동기화 + browser 모드)
#    .\start-router.ps1 -McpMode server       이전 방식
#    .\start-router.ps1 -NoSync               동기화 건너뛰기
#    .\start-router.ps1 -ModelsMax 2          모델 2개까지 동시 상주
#    .\start-router.ps1 -Build fastmtp        패치 빌드(C:\AI\llama.cpp-fastmtp) - FastMTP 사이드카 실험용
#
#  router-options.json (설정 UI 라우터 탭에서 저장) 이 있으면 명령줄에 안 준 파라미터는 거기서 읽음
#
#  사용 전: LM Studio 에서 모델을 언로드(또는 LM Studio 종료)해서 VRAM 을 비우세요.
# ============================================================
param(
    [int]$Port = 8080,
    [string]$BindHost = "127.0.0.1",
    [int]$ModelsMax = 1,
    [ValidateSet("fastmtp","official")]
    [string]$Build = "official",
    [ValidateSet("browser","server","off")]
    [string]$McpMode = "browser",
    [int]$GatewayPort = 8091,
    [int]$GatewayIdle = 600,
    [switch]$NoSync,
    [switch]$NoMcp,
    [int]$WatchInterval = 15,
    [switch]$NoWatch
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 설정 UI(config-ui, http://127.0.0.1:8092 라우터 탭)에서 저장한 값. 명령줄에서 직접 준 인자가 우선
$routerOptions = Join-Path $PSScriptRoot "router-options.json"
if (Test-Path $routerOptions) {
    try {
        $saved = Get-Content $routerOptions -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($p in $saved.PSObject.Properties) {
            if ($PSBoundParameters.ContainsKey($p.Name) -or -not $MyInvocation.MyCommand.Parameters.ContainsKey($p.Name)) { continue }
            Set-Variable -Name $p.Name -Value $p.Value
            Write-Host "[options] $($p.Name) = $($p.Value)  (router-options.json)" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "[options] router-options.json 읽기 실패 - 무시: $_" -ForegroundColor Yellow
    }
}
if ($NoMcp) { $McpMode = "off" }

if (-not $NoSync) {
    Write-Host "[sync] LM Studio -> models.ini / mcp.json / ui-config.json" -ForegroundColor DarkGray
    & python "$PSScriptRoot\sync-lmstudio.py" --gateway-port $GatewayPort
    if ($LASTEXITCODE -ne 0) { Write-Host "[sync] 실패 - 기존 파일로 계속" -ForegroundColor Yellow }
}

$fastmtpExe = Join-Path (Split-Path $PSScriptRoot -Parent) "llama.cpp-fastmtp\llama-server.exe"  # 옆 폴더의 패치 빌드
$exe = if ($Build -eq "fastmtp" -and (Test-Path $fastmtpExe)) {
    $fastmtpExe
} else {
    "$PSScriptRoot\llama-server.exe"
}

# MCP 설정이 없으면(LM Studio 미사용 등) MCP 끔. 없는 파일을 --ui-config-file / --mcp-servers-config 로 넘기면 llama-server 가 시작하자마자 종료됨
$needMcp = @()
if ($McpMode -eq "browser") { $needMcp = @("mcp.json", "ui-config.json") }
elseif ($McpMode -eq "server") { $needMcp = @("mcp.json") }
$missingMcp = @($needMcp | Where-Object { -not (Test-Path (Join-Path $PSScriptRoot $_)) })
if ($missingMcp.Count -gt 0) {
    Write-Host "[mcp] $($missingMcp -join ', ') 없음 (LM Studio MCP 설정 없음) -> MCP 끔" -ForegroundColor Yellow
    $McpMode = "off"
}

$llamaArgs = @(
    "--host", $BindHost,
    "--port", $Port,
    "--models-preset", "$PSScriptRoot\models.ini",
    "--models-max", $ModelsMax,
    "--jinja"
)

$gateway = $null
switch ($McpMode) {
    "browser" {
        # 이미 떠 있는 게이트웨이(이전 실행 잔여)가 있으면 재사용, 없으면 숨김 창으로 기동. 이 PowerShell 이 죽으면 같이 종료됨(--parent-pid)
        $busy = Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue
        if (-not $busy) {
            $gateway = Start-Process -FilePath "python" -ArgumentList @("`"$PSScriptRoot\mcp-http-gateway.py`"", "--port", $GatewayPort, "--idle", $GatewayIdle, "--parent-pid", $PID) `
                -WindowStyle Hidden -PassThru -RedirectStandardError "$PSScriptRoot\mcp-gateway.log"
            Write-Host "[mcp] gateway http://127.0.0.1:$GatewayPort/  (pid $($gateway.Id), log: mcp-gateway.log)" -ForegroundColor DarkGray
        } else {
            Write-Host "[mcp] gateway 포트 $GatewayPort 이미 사용 중 - 기존 프로세스 재사용" -ForegroundColor DarkGray
        }
        $llamaArgs += @("--ui-config-file", "$PSScriptRoot\ui-config.json")
    }
    "server" {
        $llamaArgs += @("--mcp-servers-config", "$PSScriptRoot\mcp.json")
        Write-Host "  (MCP 워밍업: 서버 18개 × 수 초 = 약 30~60초 뒤 WebUI 에 툴이 뜹니다)" -ForegroundColor DarkGray
    }
}

$watcher = $null
if (-not $NoWatch) {
    # 이전 실행에서 남은 감시 프로세스 정리 후 숨김 창으로 기동. 이 PowerShell 이 죽으면 같이 종료됨(--parent-pid)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'watch-lmstudio\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $watcher = Start-Process -FilePath "python" -ArgumentList @("`"$PSScriptRoot\watch-lmstudio.py`"", "--port", $Port, "--interval", $WatchInterval, "--parent-pid", $PID) `
        -WindowStyle Hidden -PassThru -RedirectStandardError "$PSScriptRoot\lmstudio-watch.log"
    Write-Host "[watch] LM Studio 모델 폴더 감시 ${WatchInterval}초 간격 - 새 모델은 재시작 없이 목록에 추가  (pid $($watcher.Id), log: lmstudio-watch.log)" -ForegroundColor DarkGray
}

Write-Host "[llama.cpp router] build=$Build mcp=$McpMode models-max=$ModelsMax  ->  http://${BindHost}:$Port" -ForegroundColor Cyan
if ($McpMode -eq "browser") {
    Write-Host "  WebUI 왼쪽 'MCP Servers' 에서 필요한 서버만 켜세요. (이미 쓰던 브라우저에 목록이 안 보이면: 설정 > Reset to Default 한 번)" -ForegroundColor DarkGray
}
try {
    & $exe @llamaArgs
} finally {
    if ($gateway -and -not $gateway.HasExited) { Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue }
    if ($watcher -and -not $watcher.HasExited) { Stop-Process -Id $watcher.Id -Force -ErrorAction SilentlyContinue }
}

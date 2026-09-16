# llama.cpp 설정 UI (config-ui) 설계

2026-09-16

## 목적
라우터(`start-router.ps1`, `--models-preset models.ini`)로 쓰는 모델들의 llama-server 옵션(컨텍스트, KV 캐시, 온도 등 전체)을
ini 를 직접 고치지 않고 브라우저에서 설정하고, 모델 로드/언로드와 라우터 실행 옵션까지 한 화면에서 다룬다.

## 구성
- `config-ui/server.py` : Python 표준 라이브러리만 사용. `http://127.0.0.1:8092` (8091 = MCP 게이트웨이)
  - 라우터와 별개 프로세스(pythonw, 창 없음) -> 라우터가 꺼져 있어도 열리고, 라우터를 재시작할 수 있음
  - `--open` : 이미 떠 있으면 브라우저만 열고 종료
- `config-ui/index.html` : 단일 페이지(바닐라 JS)
- `config-ui.bat` + 바탕화면 바로가기 "llama.cpp 설정"

## 데이터 흐름
- 옵션 목록: `llama-server.exe --help` 를 파싱(exe 수정시각 기준 캐시) + 프리셋 전용 키(`load-on-startup`, `stop-timeout`)
  - 이름 목록의 마지막 긍정형 long 이름 = 라우터 기준 키, 나머지는 별칭. `--no-xxx` 가 있으면 켜기/끄기 옵션
- 값의 층: llama.cpp 기본값 < `[*]` 공통 < 자동(LM Studio 동기화) < 내 설정(`models.override.ini`)
  - 자동 값은 `sync-lmstudio.py` 의 `compose_models(entries, {})` (override 없이 계산) 로 구함
- 저장: `models.override.ini` 의 해당 섹션 키 줄만 수정(주석/다른 섹션 유지, 최초 1회 `.bak`)
  -> `sync-lmstudio.py --models` -> 라우터 `GET /models?reload=1`
  - 키 철자는 override -> 자동 섹션 -> `[*]` 에 이미 쓰인 철자를 재사용(별칭 중복 방지). `!` = 자동 값 제거
  - 값에 `;` `#` 금지 (llama.cpp ini 파서가 주석으로 잘라냄), 줄바꿈 금지
- 로드/언로드: 라우터 `POST /models/load`, `/models/unload`. 상태는 `GET /models` 의 `status.value`
  (loaded / loading / unloaded / sleeping, `failed` + `exit_code`)
- 라우터 옵션: `router-options.json` (Port, ModelsMax, Build, McpMode, GatewayPort, GatewayIdle, NoSync, WatchInterval, NoWatch)
  - `start-router.ps1` 은 명령줄에서 주지 않은 파라미터를 이 파일에서 읽음 -> 기존 WebUI 바로가기에도 적용
  - 재시작: 라우터(llama-server --models-preset) 프로세스에서 부모 start-router.ps1 / start-router.bat 까지 올라가 트리 종료 후
    `start "llama.cpp router" start-router.bat`
- VRAM: `nvidia-smi --query-gpu`

## 주의
- 로드된 모델의 옵션을 바꾸면 reload 시 그 모델은 언로드됨(다음 요청 때 새 설정으로 재로드) -> 저장 전 경고
- 샘플링 값은 모델별 기본값: API 요청에 값이 없을 때 + WebUI(사용자가 WebUI 설정에서 직접 바꾼 항목 제외, parameter-sync.service.ts)
- 보안: 127.0.0.1 바인딩, Host 헤더 검사(DNS rebinding), POST 는 JSON Content-Type + Origin 검사(CSRF)

## 범위 밖
`[*]` 공통 섹션 편집, `start-single.ps1`

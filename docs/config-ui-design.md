# llama.cpp 설정 UI (config-ui) 설계

2026-09-16 (모델 소스 일반화 · 한국어/영어 추가)

## 목적
라우터(`start-router.ps1`, `--models-preset models.ini`)로 쓰는 모델들의 llama-server 옵션(컨텍스트, KV 캐시, 온도 등 전체)을
ini 를 직접 고치지 않고 브라우저에서 설정하고, 모델 소스(폴더/파일/LM Studio), 로드/언로드, 라우터 실행 옵션까지 한 화면에서 다룬다.
LM Studio 는 선택 사항 — 없어도 폴더/파일만으로 동작.

## 구성
- `config-ui/server.py` : Python 표준 라이브러리만 사용. `http://127.0.0.1:8092` (8091 = MCP 게이트웨이)
  - 라우터와 별개 프로세스(pythonw, 창 없음) -> 라우터가 꺼져 있어도 열리고, 라우터를 재시작할 수 있음
  - `--open` : 이미 떠 있으면 브라우저만 열고 종료
- `config-ui/index.html` : 단일 페이지(바닐라 JS). 탭: 모델 / 폴더 / 라우터. 언어 선택(ko/en, localStorage)
- `config-ui.bat` + 바탕화면 바로가기

## 모델 소스
- `model-folders.json` = `{"lmstudio": true, "folders": [...]}` — `sync-lmstudio.py` 가 읽음 (`model_roots()`)
  - LM Studio: 설치돼 있고 `lmstudio` 가 false 가 아니면 `~/.lmstudio/models` (+ downloadsFolder). id = LM Studio 식별자, 모델별 로드 설정 반영
  - 폴더: 하위 폴더까지 GGUF 스캔. id = 파일 이름. mmproj / 드래프터(mtp-·dspark-·dflash-, *-assistant) / MTP(nextn) 자동 감지
  - 같은 파일이 여러 소스에 걸치면 먼저 나온 소스(LM Studio)가 가짐. LM Studio 폴더 안의 폴더는 추가해도 중복 스캔 안 함
- 파일 하나: `models.override.ini` 에 `[id]` + `model = 경로` 섹션을 만듦(= 수동 추가). 추가할 때 mmproj/MTP/드래프터 키를 감지해서 같이 씀
  - mmproj·드래프터·MTP 사이드카·분할 파일 두 번째 이후 조각은 거부, id 는 `[A-Za-z0-9._@+/-]`, 중복 거부
  - 제거 = 섹션 삭제. 자동으로 찾은 모델은 제거 불가(폴더를 빼거나 LM Studio 끄기)
- `watch-lmstudio.py` 도 같은 폴더 + `model-folders.json` 을 감시

## 데이터 흐름
- 옵션 목록: `llama-server.exe --help` 를 파싱(exe 수정시각 기준 캐시) + 프리셋 전용 키(`load-on-startup`, `stop-timeout`)
  - 이름 목록의 첫 긍정형 long 이름 = UI 의 옵션 id, 나머지는 별칭. `--no-xxx` 가 있으면 켜기/끄기 옵션. 그룹 id: common / sampling / speculative / server / router
- 값의 층: llama.cpp 기본값 < `[*]` 공통 < 자동(LM Studio 설정 · 자동 감지) < 내 설정(`models.override.ini`)
  - 자동 값은 `sync-lmstudio.py` 의 `compose_models(entries, {})` (override 없이 계산) 로 구함
- 저장: `models.override.ini` 의 해당 섹션 키 줄만 수정(주석/다른 섹션 유지, 최초 1회 `.bak`)
  -> `sync-lmstudio.py --models` (실패 시 이전 파일로 되돌림) -> 라우터 `GET /models?reload=1`
  - 키 철자는 override -> 자동 섹션 -> `[*]` 에 이미 쓰인 철자를 재사용(별칭 중복 방지). `!` = 자동 값 제거
  - 값에 `;` `#` 금지 (llama.cpp ini 파서가 주석으로 잘라냄), 줄바꿈 금지
- 폴더/LM Studio 변경(`POST /api/sources`), 모델 추가(`/api/model/add`: 폴더면 소스에 추가, 파일이면 수동 섹션), 제거(`/api/model/remove`)도 같은 sync -> reload 경로
- 로드/언로드: 라우터 `POST /models/load`, `/models/unload`. 상태는 `GET /models` 의 `status.value`
  (loaded / loading / unloaded / sleeping, `failed` + `exit_code`)
- 라우터 옵션: `router-options.json` (Port, ModelsMax, Build, McpMode, GatewayPort, GatewayIdle, NoSync, WatchInterval, NoWatch)
  - `start-router.ps1` 은 명령줄에서 주지 않은 파라미터를 이 파일에서 읽음
  - MCP 파일(browser: mcp.json + ui-config.json, server: mcp.json)이 없으면 MCP 끔 — 없는 파일을 넘기면 llama-server 가 시작 시 종료됨(`read_file` 예외)
  - fastmtp 빌드 경로 = 옆 폴더 `llama.cpp-fastmtp`
  - 재시작: 라우터(llama-server --models-preset <이 폴더의 models.ini>) 프로세스에서 부모 start-router.ps1 / start-router.bat 까지 올라가 트리 종료 후
    `start "llama.cpp router" start-router.bat`
- VRAM: `nvidia-smi --query-gpu`

## 언어
- 화면 문자열은 `index.html` 의 `I18N.ko / I18N.en`, 영어 단수형은 `키.one`. 자주 쓰는 옵션 라벨/설명은 `CURATED` 에 두 언어
- 서버 메시지는 요청 헤더 `X-UI-Lang` 으로 `tr(ko, en)`. 라우터 옵션 라벨/설명·모델 NOTE 는 `{ko, en}` 로 내려줌
- `--help` 설명은 llama.cpp 원문(영어) 그대로

## 주의
- 로드된 모델의 옵션을 바꾸면 reload 시 그 모델은 언로드됨(다음 요청 때 새 설정으로 재로드) -> 저장 전 경고
- 샘플링 값은 모델별 기본값: API 요청에 값이 없을 때 + WebUI(사용자가 WebUI 설정에서 직접 바꾼 항목 제외, parameter-sync.service.ts)
- 보안: 127.0.0.1 바인딩, Host 헤더 검사(DNS rebinding), POST 는 JSON Content-Type + Origin 검사(CSRF). 거절 전에도 본문을 읽음(RST 방지)
- 네트워크 실패 / file:// 로 연 경우 안내 메시지

## 범위 밖
`[*]` 공통 섹션 편집, `start-single.ps1`, Linux / macOS (프로세스 제어가 Windows 전용)

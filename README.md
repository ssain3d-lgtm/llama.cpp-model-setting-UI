# llama.cpp model setting UI

Windows 에서 **llama.cpp 라우터(`llama-server --models-preset`)** 로 쓰는 모델들의 옵션을 브라우저에서 설정하는 로컬 UI.
LM Studio 에 받아 둔 GGUF 모델을 복사 없이 그대로 llama.cpp 로 돌리는 스크립트 세트 위에서 동작합니다.

- **모델별 전체 옵션** — 컨텍스트, KV 캐시 타입, GPU 레이어, MoE CPU 오프로드, MTP/드래프트, 온도·top-k/p·min-p·반복 패널티, 추론(thinking) …
  - 옵션 목록은 `llama-server.exe --help` 를 파싱해서 자동 생성 (llama.cpp 업데이트 시 새 옵션 자동 반영, 약 230개)
  - 자주 쓰는 옵션은 한글 설명과 함께 위에, 나머지는 검색 가능한 "전체 옵션"
  - 값마다 출처 표시: `llama.cpp 기본값` < `공통 [*]` < `LM Studio 자동` < `내 설정`
- **저장 = 적용** — `models.override.ini` 의 해당 섹션만 수정(주석 유지) → `sync-lmstudio.py` → 라우터 `/models?reload=1`
- **로드 / 언로드**, 모델 상태(로드됨·로딩·슬립·실패), VRAM 사용량
- **라우터** 시작 / 중지 / 재시작, 실행 옵션(포트, 동시 모델 수, MCP 모드, 빌드) — `router-options.json`

## 파일

| 파일 | 역할 |
|---|---|
| `config-ui/server.py`, `config-ui/index.html` | 설정 UI (Python 표준 라이브러리만, http://127.0.0.1:8092) |
| `config-ui.bat` | 창 없이 UI 서버 실행 + 브라우저 열기 (이미 떠 있으면 브라우저만) |
| `sync-lmstudio.py` | LM Studio 모델/설정/MCP → `models.ini`, `mcp.json`, `ui-config.json` 생성 (+ `models.override.ini` 병합) |
| `start-router.ps1` / `.bat` | 동기화 후 라우터 실행 (+ MCP 게이트웨이, 모델 폴더 감시). `router-options.json` 을 기본값으로 읽음 |
| `watch-lmstudio.py` | LM Studio 모델 폴더 감시 → 바뀌면 재동기화 + 라우터 reload |
| `mcp-http-gateway.py`, `mcp-lazy-proxy.py` | LM Studio MCP 서버를 WebUI 에서 쓰도록 HTTP 로 노출 |
| `open-webui.bat` | 라우터가 없으면 띄우고 WebUI 열기 |
| `docs/config-ui-design.md` | 설계 |

## 설치

1. [llama.cpp 릴리스](https://github.com/ggml-org/llama.cpp/releases) (Windows CUDA 빌드)를 한 폴더에 풀기 — 예: `C:\AI\llama.cpp`
2. 이 레포 파일을 **`llama-server.exe` 와 같은 폴더**에 복사
3. Python 3.10+ (PATH 에 `python`, `pythonw`), LM Studio 에 모델 받아 두기
4. `config-ui.bat` 실행 → 라우터 탭에서 **시작** (또는 `start-router.bat`)

생성·개인 파일(`models.ini`, `models.override.ini`, `mcp.json`, `router-options.json`, 로그)은 `.gitignore` 로 제외됩니다.

## 참고

- 로드된 모델의 옵션을 바꾸면 reload 시 그 모델은 언로드되고, 다음 요청 때 새 설정으로 다시 로드됩니다 (저장 전에 경고).
- 샘플링 값은 모델별 **기본값**입니다. API 요청에 값이 없을 때 쓰이고, llama.cpp WebUI 는 WebUI 설정에서 직접 바꾼 항목이 우선합니다.
- llama.cpp 프리셋 ini 는 값 안의 `;` `#` 를 주석으로 잘라내므로 UI 가 입력을 막습니다 (샘플러 순서는 `sampler-seq` 사용).
- 127.0.0.1 전용. Host / Origin / JSON Content-Type 검사로 다른 사이트에서의 요청을 막습니다.

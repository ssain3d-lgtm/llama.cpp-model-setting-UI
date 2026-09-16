# llama.cpp model setting UI

**English** · [한국어](#한국어)

A local web UI for Windows to configure every `llama-server` option per model when you run llama.cpp in **router mode** (`llama-server --models-preset`), plus loading / unloading models and starting the router.
Models come from your own **GGUF folders**, single GGUF files, and — optionally — **LM Studio** (its downloaded models and per-model load settings are reused without copying).

- **All options, per model** — context size, KV cache type, GPU layers, MoE CPU offload, MTP / draft models, temperature, top-k/p, min-p, repeat penalty, reasoning, …
  - The option list is generated from `llama-server.exe --help`, so new options appear automatically when you update llama.cpp (~230 today)
  - Common options first with plain-language descriptions, everything else in a searchable "All options" section
  - Every value shows where it comes from: `llama.cpp default` < `Global [*]` < `Auto-detected / LM Studio` < `custom`
- **Save = apply** — only that model's section of `models.override.ini` is rewritten (comments kept) → `sync-lmstudio.py` → router `/models?reload=1`
- **Model sources** — add folders (scanned recursively), add single GGUF files, turn LM Studio on/off. mmproj (vision), drafters and MTP heads in the same folder are detected automatically
- **Load / unload**, live model status (loaded · loading · sleeping · failed), VRAM usage (NVIDIA)
- **Router** start / stop / restart and launch options (port, models kept loaded, MCP mode, build)
- **Korean / English** UI (language selector in the top-right corner)

## Requirements

- Windows 10 / 11
- A recent [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases) for Windows that supports router mode (`--models-preset`)
- Python 3.8+ with `python` and `pythonw` on PATH (standard library only, nothing to `pip install`)
- Optional: NVIDIA GPU (VRAM bar uses `nvidia-smi`), LM Studio

## Install

1. Extract the llama.cpp Windows release into a folder, e.g. `C:\AI\llama.cpp`
2. Download this repository (**Code → Download ZIP**) and extract its files into **the same folder as `llama-server.exe`**
3. Run `config-ui.bat` — the settings page opens at http://127.0.0.1:8092
4. **Folders** tab → add your models folder (or **+ Add model** for a single `.gguf`). LM Studio models show up automatically if LM Studio is installed
5. **Router** tab → **Start**. The llama.cpp WebUI / OpenAI-compatible API is at http://127.0.0.1:8080

## Files

| File | Role |
|---|---|
| `config-ui/server.py`, `config-ui/index.html` | Settings UI server and page |
| `config-ui.bat` | Starts the UI server without a window and opens the browser (only opens the browser if it is already running) |
| `sync-lmstudio.py` | Scans model folders (+ LM Studio) → writes `models.ini` (router preset), merging `models.override.ini`; converts LM Studio MCP settings |
| `start-router.ps1` / `.bat` | Sync, then start the router (+ MCP gateway, folder watcher). Reads `router-options.json` for parameters not given on the command line |
| `watch-lmstudio.py` | Watches model folders / LM Studio → re-sync and reload the router when models change |
| `mcp-http-gateway.py`, `mcp-lazy-proxy.py` | Expose LM Studio MCP servers to the llama.cpp WebUI over HTTP |
| `open-webui.bat` | Starts the router if needed and opens the WebUI (port 8080) |
| `docs/config-ui-design.md` | Design notes |

Created at runtime and not tracked: `models.ini` (generated), `models.override.ini` (your custom values), `model-folders.json` (model sources), `router-options.json` (router options), `mcp.json` / `ui-config.json` (MCP), logs.

## Notes

- Changing options of a **loaded** model unloads it on apply; it reloads with the new settings on the next request (the UI warns first).
- Sampling values are **per-model defaults**: used when a request does not set them. In the llama.cpp WebUI, values you changed in its own settings take precedence.
- llama.cpp's preset parser treats `;` and `#` inside values as comments, so the UI rejects them (use `sampler-seq` for sampler order).
- MCP is taken from LM Studio's `mcp.json`. Without it, the router starts with MCP off automatically.
- `open-webui.bat` assumes port 8080. The Build option `fastmtp` looks for a patched build in the sibling folder `llama.cpp-fastmtp` and falls back to the official build.
- The UI listens on 127.0.0.1 only and rejects requests from other sites (Host / Origin / JSON content-type checks). Log: `config-ui.log`.

---

## 한국어

Windows 에서 llama.cpp 를 **라우터 모드**(`llama-server --models-preset`)로 쓸 때, 모델마다 `llama-server` 옵션 전부를 브라우저에서 설정하고 모델 로드/언로드와 라우터 실행까지 다루는 로컬 UI 입니다.
모델은 **내 GGUF 폴더**, GGUF 파일 하나씩, 그리고 (선택) **LM Studio** 에서 가져옵니다. LM Studio 모델과 모델별 로드 설정은 복사 없이 그대로 씁니다.

- **모델별 전체 옵션** — 컨텍스트, KV 캐시 타입, GPU 레이어, MoE CPU 오프로드, MTP/드래프트, 온도·top-k/p·min-p·반복 패널티, 추론(thinking) …
  - 옵션 목록은 `llama-server.exe --help` 에서 자동 생성 (llama.cpp 업데이트 시 새 옵션 자동 반영, 현재 약 230개)
  - 자주 쓰는 옵션은 설명과 함께 위에, 나머지는 검색 가능한 "전체 옵션"
  - 값마다 출처 표시: `llama.cpp 기본값` < `공통 [*]` < `자동 감지 / LM Studio` < `내 설정`
- **저장 = 적용** — `models.override.ini` 의 해당 모델 섹션만 수정(주석 유지) → `sync-lmstudio.py` → 라우터 `/models?reload=1`
- **모델 소스** — 폴더 추가(하위 폴더까지 스캔), GGUF 파일 하나 추가, LM Studio 켜기/끄기. 같은 폴더의 mmproj(비전)·드래프터·MTP 는 자동 감지
- **로드 / 언로드**, 모델 상태(로드됨·로딩·슬립·실패), VRAM 사용량(NVIDIA)
- **라우터** 시작 / 중지 / 재시작, 실행 옵션(포트, 동시 모델 수, MCP 모드, 빌드)
- **한국어 / English** (오른쪽 위 언어 선택)

## 필요 환경

- Windows 10 / 11
- 라우터 모드(`--models-preset`)를 지원하는 최신 [llama.cpp 릴리스](https://github.com/ggml-org/llama.cpp/releases) (Windows 빌드)
- Python 3.8+ (`python`, `pythonw` 가 PATH 에 있어야 함, 표준 라이브러리만 사용 — pip 설치 없음)
- 선택: NVIDIA GPU (VRAM 표시는 `nvidia-smi` 사용), LM Studio

## 설치

1. llama.cpp Windows 릴리스를 폴더에 풀기 — 예: `C:\AI\llama.cpp`
2. 이 레포를 **Code → Download ZIP** 으로 받아서 **`llama-server.exe` 와 같은 폴더**에 풀기
3. `config-ui.bat` 실행 → http://127.0.0.1:8092 설정 화면이 열림
4. **폴더** 탭에서 모델 폴더 추가 (파일 하나면 **+ 모델 추가**). LM Studio 가 설치되어 있으면 그 모델은 자동으로 보임
5. **라우터** 탭 → **시작**. llama.cpp WebUI / OpenAI 호환 API 는 http://127.0.0.1:8080

## 파일

| 파일 | 역할 |
|---|---|
| `config-ui/server.py`, `config-ui/index.html` | 설정 UI 서버와 화면 |
| `config-ui.bat` | 창 없이 UI 서버 실행 + 브라우저 열기 (이미 떠 있으면 브라우저만) |
| `sync-lmstudio.py` | 모델 폴더(+ LM Studio) 스캔 → `models.ini`(라우터 프리셋) 생성, `models.override.ini` 병합, LM Studio MCP 설정 변환 |
| `start-router.ps1` / `.bat` | 동기화 후 라우터 실행 (+ MCP 게이트웨이, 폴더 감시). 명령줄에 안 준 파라미터는 `router-options.json` 에서 읽음 |
| `watch-lmstudio.py` | 모델 폴더 / LM Studio 감시 → 모델이 바뀌면 재동기화 + 라우터 reload |
| `mcp-http-gateway.py`, `mcp-lazy-proxy.py` | LM Studio MCP 서버를 llama.cpp WebUI 에서 쓰도록 HTTP 로 노출 |
| `open-webui.bat` | 라우터가 없으면 띄우고 WebUI 열기 (포트 8080) |
| `docs/config-ui-design.md` | 설계 |

실행 중 생기고 레포에는 안 올라가는 파일: `models.ini`(자동 생성), `models.override.ini`(내 설정), `model-folders.json`(모델 소스), `router-options.json`(라우터 옵션), `mcp.json` / `ui-config.json`(MCP), 로그.

## 참고

- **로드된 모델**의 옵션을 바꾸면 적용할 때 언로드되고, 다음 요청 때 새 설정으로 다시 로드됩니다 (UI 가 먼저 경고).
- 샘플링 값은 **모델별 기본값**입니다. 요청에 값이 없을 때 쓰이고, llama.cpp WebUI 는 WebUI 설정에서 직접 바꾼 항목이 우선합니다.
- llama.cpp 프리셋 파서는 값 안의 `;` `#` 를 주석으로 잘라내므로 UI 가 입력을 막습니다 (샘플러 순서는 `sampler-seq`).
- MCP 는 LM Studio 의 `mcp.json` 에서 가져옵니다. 없으면 라우터가 자동으로 MCP 없이 시작합니다.
- `open-webui.bat` 은 포트 8080 기준입니다. 빌드 옵션 `fastmtp` 는 옆 폴더 `llama.cpp-fastmtp` 의 패치 빌드를 쓰고, 없으면 공식 빌드를 씁니다.
- UI 는 127.0.0.1 에서만 열리고 다른 사이트에서 오는 요청은 막습니다 (Host / Origin / JSON 검사). 로그: `config-ui.log`.

# -*- coding: utf-8 -*-
"""
LM Studio(있으면) + 모델 폴더 -> llama.cpp 동기화
  python sync-lmstudio.py            models.ini + mcp.json 둘 다 재생성
  python sync-lmstudio.py --models   models.ini 만
  python sync-lmstudio.py --mcp      mcp.json 만
  python sync-lmstudio.py --dry-run  파일을 쓰지 않고 결과만 출력

models.ini (llama-server --models-preset)
  - LM Studio 모델 폴더 + model-folders.json 의 폴더에서 모든 LLM GGUF 를 하위 폴더까지 스캔 (복사 안 함, 경로만 참조)
      model-folders.json = {"lmstudio": true, "folders": ["D:/models", ...]}   (설정 UI 의 '폴더' 탭에서 편집)
      LM Studio 가 없거나 "lmstudio": false 면 폴더만 사용. 폴더 모델의 id 는 파일 이름에서 만듦
  - 모델 id = LM Studio API 식별자 (model-index-cache.json 의 defaultIdentifier, 예: qwen3.8-27b)
    -> LM Studio 용으로 맞춰 둔 외부 앱의 "model" 값을 그대로 쓸 수 있음
  - 같은 폴더의 mmproj-*.gguf 자동 연결 (비전)
  - GGUF 헤더에서 MTP(nextn) 레이어 감지 -> spec-type = draft-mtp 자동 부여
  - 같은 폴더의 드래프터(LM Studio domain=drafter, 예: mtp-gemma-4-*.gguf) -> spec-draft-model 자동 연결
  - MTP 헤드만 든 사이드카(예: *-FastMTP-32K.gguf, 본체 블록 없음)는 단독 모델로 올리지 않음
  - LM Studio 의 모델별 로드 설정(컨텍스트 길이, KV 캐시 양자화, 병렬 수, 플래시어텐션 ...) 을 그대로 반영
    (.internal/user-concrete-model-default-config/<모델>.gguf.json)
    설정이 없는 모델은 LM Studio 기본 컨텍스트(settings.json defaultContextLength) 사용
  - LM Studio 의 JIT TTL(settings.json developer.jitModelTTL) -> sleep-idle-seconds 로 반영
  - models.override.ini 가 있으면 마지막에 덮어씀:
      [*]            전역 기본값 덮어쓰기/추가
      [<모델 id>]    해당 모델 키 덮어쓰기/추가  (예: ctx-size 줄이기)
      [기타 섹션]    그대로 추가 (예: FastMTP 사이드카 실험 항목)
      키 = !        로 쓰면 그 키를 제거

ui-config.json (llama-server --ui-config-file) + webui-settings-import.json
  - WebUI 의 브라우저쪽 MCP 클라이언트용 서버 목록 (mcp-http-gateway.py 의 http://127.0.0.1:<port>/<name>/mcp)
    -> WebUI 에서 LM Studio 처럼 서버 단위 on/off, 대화별 선택 가능. 처음 여는 브라우저에는 자동 적용,
       이미 쓰던 브라우저는 설정 > Reset to Default 한 번 (또는 Import/Export 탭에서 webui-settings-import.json Import)
  - --gateway-port N 으로 포트 변경 (기본 8091)

mcp.json (llama-server --mcp-servers-config / mcp-http-gateway.py 공용)
  - ~/.lmstudio/mcp.json 을 그대로 변환
  - npx/npm 계열은 Windows 에서 cmd /c 로 래핑 (없으면 spawn 실패)
  - url 타입(HTTP) 서버는 llama-server 가 stdio 만 지원하므로 "npx -y mcp-remote <url> [--header K:V]" 로 래핑
  - mcp.override.json 이 있으면 서버별로 병합 (timeout_ms 늘리기, 비활성화 등)
      { "mcpServers": { "crawl4ai": { "timeout_ms": 120000 }, "comfyui": { "disabled": true } } }
"""
import json, os, re, struct, sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = set(a for a in sys.argv[1:])
DRY = "--dry-run" in ARGS
DO_MODELS = "--models" in ARGS or "--mcp" not in ARGS
DO_MCP = "--mcp" in ARGS or "--models" not in ARGS
GATEWAY_PORT = int(sys.argv[sys.argv.index("--gateway-port") + 1]) if "--gateway-port" in sys.argv else 8091


def lm_home():
    p = os.path.expanduser("~/.lmstudio-home-pointer")
    if os.path.isfile(p):
        v = open(p, encoding="utf-8").read().strip()
        if v and os.path.isdir(v):
            return v
    return os.path.expanduser("~/.lmstudio")


LM = lm_home()
HAS_LM = os.path.isdir(LM)
SETTINGS = {}
if HAS_LM:
    try:
        SETTINGS = json.load(open(os.path.join(LM, "settings.json"), encoding="utf-8"))
    except Exception as e:
        print(f"[warn] settings.json 읽기 실패: {e}")
FOLDERS_FILE = os.path.join(HERE, "model-folders.json")


def norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def fwd(p):
    return os.path.abspath(p).replace("\\", "/")


def under(p, root):
    p, root = norm(p), norm(root)
    return p == root or p.startswith(root.rstrip("\\/") + os.sep)


def load_folders():
    """model-folders.json -> (LM Studio 모델 포함 여부, [모델 폴더])"""
    try:
        d = json.load(open(FOLDERS_FILE, encoding="utf-8-sig"))
    except FileNotFoundError:
        return True, []
    except Exception as e:
        print(f"[warn] model-folders.json 읽기 실패(무시): {e}")
        return True, []
    folders = [os.path.abspath(os.path.expanduser(f)) for f in d.get("folders", []) if isinstance(f, str) and f.strip()]
    return d.get("lmstudio", True) is not False, folders


# ----------------------------------------------------------------------------- GGUF header
GGUF_TYPES = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


def read_gguf_header(path, want_keys=(), max_tensors=4096):
    """헤더만 읽어서 {kv: {...}, tensors: [names], n_tensors} 반환. 텐서 데이터는 읽지 않음."""
    out = {"kv": {}, "tensors": [], "n_tensors": 0}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        ver = struct.unpack("<I", f.read(4))[0]
        if ver < 2:
            raise ValueError(f"unsupported GGUF version {ver}")
        n_tensors, n_kv = struct.unpack("<QQ", f.read(16))
        out["n_tensors"] = n_tensors

        def rstr():
            n = struct.unpack("<Q", f.read(8))[0]
            return f.read(n).decode("utf-8", "replace")

        def rval(t):
            if t in GGUF_TYPES:
                fmt = GGUF_TYPES[t]
                return struct.unpack("<" + fmt, f.read(struct.calcsize(fmt)))[0]
            if t == 8:
                return rstr()
            if t == 9:
                et, n = struct.unpack("<IQ", f.read(12))
                if et == 8:
                    vals = [rstr() for _ in range(n)]
                elif et in GGUF_TYPES:
                    fmt = GGUF_TYPES[et]
                    sz = struct.calcsize(fmt)
                    vals = list(struct.unpack("<" + fmt * n, f.read(sz * n))) if n else []
                else:
                    raise ValueError(f"bad array elem type {et}")
                return vals
            raise ValueError(f"bad kv type {t}")

        for _ in range(n_kv):
            k = rstr()
            t = struct.unpack("<I", f.read(4))[0]
            v = rval(t)
            if t == 9 and len(v) > 64 and not k.endswith("nextn_predict_layers"):
                v = v[:8] + ["..."]  # 토크나이저 배열 등은 잘라서 보관
            out["kv"][k] = v
        for _ in range(min(n_tensors, max_tensors)):
            name = rstr()
            nd = struct.unpack("<I", f.read(4))[0]
            f.read(8 * nd + 4 + 8)
            out["tensors"].append(name)
    return out


def inspect_gguf(path):
    h = read_gguf_header(path)
    kv = h["kv"]
    arch = kv.get("general.architecture", "")
    info = {
        "arch": arch,
        "type": kv.get("general.type", "model"),
        "name": kv.get("general.name", ""),
        "ctx_train": kv.get(f"{arch}.context_length"),
        "mtp": False,
        "split_count": kv.get("split.count", 1),
        "split_no": kv.get("split.no", 0),
        "file_type": kv.get("general.file_type"),
    }
    nextn = kv.get(f"{arch}.nextn_predict_layers")
    if isinstance(nextn, (int, float)) and nextn > 0:
        info["mtp"] = True
    if not info["mtp"] and any(".nextn." in t or t.startswith("nextn.") for t in h["tensors"]):
        info["mtp"] = True
    # MTP 헤드만 뽑아낸 사이드카 (예: FastMTP-32K): nextn 은 있는데 본체 블록(blk.0.*)이 없음 -> 단독 로드 불가
    blocks = kv.get(f"{arch}.block_count")
    info["sidecar"] = bool(info["mtp"] and isinstance(blocks, int) and isinstance(nextn, int) and blocks > nextn
                           and not any(t.startswith("blk.0.") for t in h["tensors"]))
    return info


# ----------------------------------------------------------------------------- LM Studio index / configs
def load_index():
    """model-index-cache.json -> {normpath: entry}"""
    idx = {}
    p = os.path.join(LM, ".internal", "model-index-cache.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print(f"[warn] model-index-cache.json 읽기 실패 ({e}) -> 파일명 기반 id 사용")
        return idx
    for m in d.get("models", []):
        root = m.get("containingDirAbsolutePath")
        fn = m.get("file")
        files = m.get("files") or []
        if root and fn:
            idx[norm(os.path.join(root, fn))] = m
        for fi in files:
            ap = fi.get("absPath")
            if ap:
                idx.setdefault(norm(ap), m)
    return idx


def lm_model_config(rel_key):
    """user-concrete-model-default-config/<publisher>/<repo>/<file>.gguf.json -> {key: value}"""
    p = os.path.join(LM, ".internal", "user-concrete-model-default-config", rel_key + ".json")
    if not os.path.isfile(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for sec in ("load", "operation"):
        for f in (d.get(sec) or {}).get("fields", []):
            out[f.get("key")] = f.get("value")
    return out


def lm_default_ctx():
    d = SETTINGS.get("defaultContextLength") or {}
    if d.get("type") == "custom" and isinstance(d.get("value"), int):
        return d["value"]
    return None  # llama.cpp 기본값 사용


def lm_jit_ttl():
    t = (SETTINGS.get("developer") or {}).get("jitModelTTL") or {}
    if t.get("enabled") and isinstance(t.get("ttlSeconds"), int) and t["ttlSeconds"] > 0:
        return t["ttlSeconds"]
    return None


def checked(v):
    """LM Studio 의 {checked: bool, value: x} 필드 -> x 또는 None"""
    if isinstance(v, dict):
        return v.get("value") if v.get("checked") else None
    return v


def lm_cfg_to_args(cfg, ctx_train):
    """LM Studio 로드 설정 -> llama-server 인자"""
    a = {}
    ctx = cfg.get("llm.load.contextLength")
    if isinstance(ctx, int) and ctx > 0:
        if isinstance(ctx_train, int) and ctx > ctx_train:
            ctx = ctx_train
        a["ctx-size"] = ctx
    k = checked(cfg.get("llm.load.llama.kCacheQuantizationType"))
    v = checked(cfg.get("llm.load.llama.vCacheQuantizationType"))
    if k:
        a["cache-type-k"] = k
    if v:
        a["cache-type-v"] = v
    par = cfg.get("llm.load.numParallelSessions")
    if isinstance(par, int) and par > 0:
        a["parallel"] = par
    thr = cfg.get("llm.load.llama.cpuThreadPoolSize")
    if isinstance(thr, int) and thr > 0:
        a["threads"] = thr
    fa = cfg.get("llm.load.llama.flashAttention")
    if isinstance(fa, bool):
        a["flash-attn"] = "on" if fa else "off"
    bs = cfg.get("llm.load.llama.evalBatchSize")
    if isinstance(bs, int) and bs > 0:
        a["batch-size"] = bs
    off = cfg.get("llm.load.llama.acceleration.offloadRatio")
    if isinstance(off, (int, float)) and off < 1:
        a["_note_offload"] = off  # 레이어 수를 모르므로 주석으로만 남김
    rb = checked(cfg.get("llm.load.llama.ropeFrequencyBase"))
    rs = checked(cfg.get("llm.load.llama.ropeFrequencyScale"))
    if isinstance(rb, (int, float)) and rb > 0:
        a["rope-freq-base"] = rb
    if isinstance(rs, (int, float)) and rs > 0:
        a["rope-freq-scale"] = rs
    if cfg.get("llm.load.llama.keepModelInMemory") is True:
        a["mlock"] = True
    if cfg.get("llm.load.llama.tryMmap") is False:
        a["no-mmap"] = True
    ne = cfg.get("llm.load.numExperts")
    if isinstance(ne, int) and ne > 0:
        a["_note_experts"] = ne
    return a


# ----------------------------------------------------------------------------- scan
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
DRAFT_RE = re.compile(r"^(mtp|dspark|dflash)-", re.I)  # llama.cpp --models-dir 와 같은 드래프터 접두사


def model_roots():
    """[(폴더, 'lmstudio' | 'folder')]"""
    use_lm, folders = load_folders()
    roots = []
    if use_lm and HAS_LM:
        base = os.path.join(LM, "models")
        if os.path.isdir(base):
            roots.append((base, "lmstudio"))
        dl = SETTINGS.get("downloadsFolder")
        if dl and os.path.isdir(dl) and not under(dl, base):
            roots.append((dl, "lmstudio"))
    for f in folders:
        if not os.path.isdir(f):
            print(f"[warn] 모델 폴더 없음(건너뜀): {f}")
        elif not any(under(f, r) for r, _ in roots):
            roots.append((f, "folder"))
    return roots


def scan_models():
    """(roots, [(gguf 경로, root, kind)])"""
    roots = model_roots()
    seen = set()
    ggufs = []
    for root, kind in roots:
        for dp, dn, fn in os.walk(root):
            for f in fn:
                if f.lower().endswith(".gguf"):
                    p = norm(os.path.join(dp, f))
                    if p not in seen:
                        seen.add(p)
                        ggufs.append((os.path.join(dp, f), root, kind))
    return roots, sorted(ggufs)


def pick_mmproj(model_path, mmprojs):
    if not mmprojs:
        return None
    if len(mmprojs) == 1:
        return mmprojs[0]
    base = os.path.basename(model_path).lower()

    def score(p):
        n = os.path.basename(p).lower()
        stem = n[len("mmproj-"):] if n.startswith("mmproj-") else n
        common = 0
        for x, y in zip(stem, base):
            if x != y:
                break
            common += 1
        prec = 2 if ("bf16" in n or "f16" in n) else (1 if "f32" in n else 0)
        return (common, prec)

    return sorted(mmprojs, key=score, reverse=True)[0]


def to_id(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9._@:+/-]+", "-", s)
    return s.strip("-")


def build_models():
    roots, ggufs = scan_models()
    index = load_index() if any(kind == "lmstudio" for _, kind in roots) else {}
    by_dir = {}
    for g, _, _ in ggufs:
        by_dir.setdefault(os.path.dirname(g), []).append(g)

    entries = []
    skipped = []
    drafters = {}  # dir -> [드래프터 GGUF]  (LM Studio domain=drafter, gemma4-assistant, mtp-/dspark-/dflash- 접두사)
    for g, groot, kind in ggufs:
        fn = os.path.basename(g)
        if fn.lower().startswith("mmproj"):
            continue
        m = SPLIT_RE.search(fn)
        if m and int(m.group(1)) != 1:
            continue  # 분할 파일은 첫 조각만
        try:
            info = inspect_gguf(g)
        except Exception as e:
            skipped.append((g, f"GGUF 헤더 읽기 실패: {e}"))
            continue
        if info["type"] in ("mmproj", "clip") or info["arch"] in ("clip", "mmproj"):
            continue
        ie = index.get(norm(g), {})
        domain = ie.get("domain", "llm")
        if domain == "drafter" or info["arch"].endswith("-assistant") or DRAFT_RE.match(fn):
            drafters.setdefault(os.path.dirname(g), []).append(g)
            continue
        if info["sidecar"]:
            skipped.append((g, "MTP 사이드카(본체 레이어 없음) - 단독 로드 불가"))
            continue
        if domain not in ("llm", "embedding"):
            skipped.append((g, f"domain={domain}"))
            continue

        # id
        mid = ie.get("defaultIdentifier")
        quant = None
        bai = ie.get("baselessAutoIdentifiers") or []
        if bai and bai[0] not in ("?",):
            quant = bai[0]
        if not mid:
            mid = to_id(re.sub(r"\.gguf$", "", SPLIT_RE.sub(".gguf", fn), flags=re.I))
        # LM Studio 상대 키 (user-concrete-model-default-config 용). 폴더 모델은 LM Studio 설정 없음
        cfg = {}
        if kind == "lmstudio":
            sub = ie.get("containingDirSubpath")
            if sub and ie.get("file"):
                rel = sub.replace("\\", "/") + "/" + ie["file"]
            else:
                # downloadsFolder 가 models 하위인 경우 (예: models/<폴더>/...) 첫 폴더를 떼고도 시도
                rel = os.path.relpath(g, groot).replace("\\", "/")
            cfg = lm_model_config(rel)
            if not cfg and "/" in rel:
                cfg = lm_model_config(rel.split("/", 1)[1])

        mmprojs = [p for p in by_dir.get(os.path.dirname(g), []) if os.path.basename(p).lower().startswith("mmproj")]
        entries.append({
            "id": mid, "quant": quant, "path": g, "info": info, "domain": domain,
            "mmproj": pick_mmproj(g, mmprojs) if domain == "llm" else None,
            "lm_args": lm_cfg_to_args(cfg, info["ctx_train"]),
            "display": ie.get("displayName") or info["name"] or mid,
            "index_ctx": ie.get("contextLength"),
            "draft": None,
            "source": kind, "root": groot,
        })

    # 같은 폴더의 드래프터 -> spec-draft-model (내장 MTP 헤드가 있는 모델은 그걸 쓰므로 제외)
    for e in entries:
        ds = drafters.get(os.path.dirname(e["path"]))
        if ds and e["domain"] == "llm" and not e["info"]["mtp"]:
            e["draft"] = ds[0]

    # id 충돌 -> @quant 붙이기 (LM Studio 와 동일한 규칙)
    counts = {}
    for e in entries:
        counts[e["id"]] = counts.get(e["id"], 0) + 1
    used = set()
    for e in entries:
        if counts[e["id"]] > 1 and e["quant"]:
            e["id"] = f"{e['id']}@{e['quant']}"
        base = e["id"]
        n = 2
        while e["id"] in used:
            e["id"] = f"{base}:{n}"
            n += 1
        used.add(e["id"])
    entries.sort(key=lambda e: e["id"])
    return entries, skipped, roots


# ----------------------------------------------------------------------------- ini
def parse_ini(path):
    """아주 단순한 ini 파서: {section: [(key, value), ...]} (순서 유지). ';' '#' 주석."""
    secs = {}
    order = []
    cur = None
    if not os.path.isfile(path):
        return secs, order
    for line in open(path, encoding="utf-8-sig"):
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            if cur not in secs:
                secs[cur] = []
                order.append(cur)
            continue
        if "=" in s and cur is not None:
            k, v = s.split("=", 1)
            secs[cur].append((k.strip(), v.strip()))
    return secs, order


def apply_override(sec_dict, overrides):
    for k, v in overrides:
        if v == "!":
            sec_dict.pop(k, None)
        else:
            sec_dict[k] = v


def fmt_val(v):
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def compose_models(entries, ov):
    """[*] 전역값 dict 와 [(entry, 섹션 dict, NOTE 목록)] 반환. ov = parse_ini 결과 ({} 면 override 없이 = 자동 값만).
    write_models_ini 와 설정 UI(config-ui/server.py) 가 같이 씀."""
    glob = {
        "n-gpu-layers": "all",
        "flash-attn": "on",
        "batch-size": 2048,
        "ubatch-size": 512,
        "parallel": 1,
        "jinja": "true",
    }
    dctx = lm_default_ctx()
    if dctx:
        glob["ctx-size"] = dctx
    ttl = lm_jit_ttl()
    if ttl:
        glob["sleep-idle-seconds"] = ttl
    apply_override(glob, ov.get("*", []))

    models = []
    for e in entries:
        info = e["info"]
        sec = {}
        sec["model"] = fwd(e["path"])
        if e["mmproj"]:
            sec["mmproj"] = fwd(e["mmproj"])
        if e["draft"]:
            sec["spec-draft-model"] = fwd(e["draft"])
            # 자동 판별은 blk.<last>.nextn.eh_proj 텐서만 봐서 gemma4-assistant 드래프터는 못 잡음 -> 명시 (gemma4 는 MTP 경로, is_mem_shared)
            sec["spec-type"] = "draft-mtp"
            sec["spec-draft-n-max"] = 3
        if e["domain"] == "embedding":
            sec["embeddings"] = "true"
            sec["ctx-size"] = min(8192, info["ctx_train"] or 8192)
        notes = []
        for k, v in e["lm_args"].items():
            if k == "_note_offload":
                notes.append(f"LM Studio offloadRatio={v} (부분 오프로드) -> 여기선 n-gpu-layers 로 직접 지정 필요")
            elif k == "_note_experts":
                notes.append(f"LM Studio numExperts={v} -> 필요시 override-kv {info['arch']}.expert_used_count=int:{v}")
            else:
                sec[k] = v
        if info["mtp"]:
            sec["spec-type"] = "draft-mtp"
            sec["spec-draft-n-max"] = 3
        apply_override(sec, ov.get(e["id"], []))
        models.append((e, sec, notes))
    return glob, models


def write_models_ini(entries, skipped, roots):
    ov, ov_order = parse_ini(os.path.join(HERE, "models.override.ini"))
    glob, models = compose_models(entries, ov)

    lines = []
    L = lines.append
    L("version = 1")
    L("")
    L("; =====================================================================================")
    L(";  이 파일은 sync-lmstudio.py 가 자동 생성합니다. 직접 고치지 말고 models.override.ini 를 쓰세요.")
    L(";  모델 소스: " + (" | ".join(fwd(r) for r, _ in roots) or "(없음)") + "  (LM Studio 폴더를 그대로 참조, 복사 안 함)")
    L(";  섹션명 = 모델 id (API 'model' 필드 / WebUI 드롭다운) = LM Studio API 식별자와 동일")
    L(";  키 = llama-server 인자(앞의 -- 제외).  ctx-size / cache-type-k,v 등은 LM Studio 모델별 로드 설정에서 복사")
    L(";  sleep-idle-seconds = LM Studio JIT TTL 과 동일: 이 시간 동안 요청이 없으면 VRAM 해제(다음 요청 시 자동 재로드)")
    L("; =====================================================================================")
    L("")
    L("[*]")
    for k, v in glob.items():
        L(f"{k} = {fmt_val(v)}")
    L("")

    for e, sec, notes in models:
        info = e["info"]
        size_gb = os.path.getsize(e["path"]) / 1e9
        L(f"; {e['display']}  |  arch={info['arch']}  {size_gb:.1f}GB  train-ctx={info['ctx_train']}  mtp={'yes' if info['mtp'] else 'no'}  vision={'yes' if e['mmproj'] else 'no'}  draft={os.path.basename(e['draft']) if e['draft'] else 'no'}")
        for n in notes:
            L(f"; NOTE: {n}")
        L(f"[{e['id']}]")
        for k, v in sec.items():
            L(f"{k} = {fmt_val(v)}")
        L("")

    # override 파일의 기타 섹션은 그대로 추가
    known = {"*"} | {e["id"] for e in entries}
    extra = [s for s in ov_order if s not in known]
    if extra:
        L("; ---------- models.override.ini 에서 추가된 항목 ----------")
        for s in extra:
            L(f"[{s}]")
            for k, v in ov[s]:
                L(f"{k} = {v}")
            L("")

    text = "\n".join(lines) + "\n"
    dst = os.path.join(HERE, "models.ini")
    if DRY:
        print(text)
    else:
        open(dst, "w", encoding="utf-8").write(text)
    print(f"[models] {len(entries)}개 모델 -> {dst}" + ("  (dry-run, 미저장)" if DRY else ""))
    for e in entries:
        i = e["info"]
        print(f"   {e['id']:50s} ctx={e['lm_args'].get('ctx-size', glob.get('ctx-size', 'default'))!s:7s} "
              f"{'MTP ' if i['mtp'] else '    '}{'DRF ' if e['draft'] else '    '}{'VIS ' if e['mmproj'] else '    '}{os.path.basename(e['path'])}")
    for g, why in skipped:
        print(f"   [skip] {g}: {why}")
    if extra:
        print(f"   + override 추가 섹션: {', '.join(extra)}")


# ----------------------------------------------------------------------------- mcp
def build_mcp():
    src_p = os.path.join(LM, "mcp.json")
    if not os.path.isfile(src_p):
        print(f"[mcp] LM Studio mcp.json 없음 ({src_p}) - MCP 동기화 건너뜀")
        return
    try:
        src = json.load(open(src_p, encoding="utf-8"))
    except Exception as e:
        print(f"[mcp] {src_p} 읽기 실패: {e}")
        return
    ov = {}
    ovp = os.path.join(HERE, "mcp.override.json")
    if os.path.isfile(ovp):
        try:
            ov = json.load(open(ovp, encoding="utf-8")).get("mcpServers", {})
        except Exception as e:
            print(f"[mcp] mcp.override.json 파싱 실패(무시): {e}")

    out = {"mcpServers": {}}
    defaults = ov.get("*", {})
    proxy = os.path.join(HERE, "mcp-lazy-proxy.py")
    for name, cfg in src.get("mcpServers", {}).items():
        o = dict(defaults)
        o.update(ov.get(name, {}))
        if o.get("disabled") or cfg.get("disabled"):
            print(f"  - {name:20s} 비활성화")
            continue
        if cfg.get("command"):
            cmd, args = cfg["command"], list(cfg.get("args", []))
            if cmd.lower() in ("npx", "npx.cmd", "npm", "npm.cmd", "node.cmd", "pnpm", "pnpm.cmd", "bunx", "bunx.cmd"):
                cmd, args = "cmd", ["/c", cmd.replace(".cmd", "")] + args
            entry = {"command": cmd, "args": args}
            if cfg.get("env"):
                entry["env"] = dict(cfg["env"])
            if cfg.get("cwd"):
                entry["cwd"] = cfg["cwd"]
            kind = "stdio"
        elif cfg.get("url"):
            args = ["-y", "mcp-remote", cfg["url"]]
            for hk, hv in (cfg.get("headers") or {}).items():
                args += ["--header", f"{hk}: {hv}"]
            entry = {"command": "cmd", "args": ["/c", "npx"] + args}
            kind = "http->mcp-remote"
        else:
            print(f"  - {name:20s} SKIP (command/url 없음)")
            continue
        lazy = False
        for k, v in o.items():
            if k in ("disabled", "lazy", "lazy_wait", "exclude_tools", "strip_schema_keys"):
                lazy = lazy or (k in ("lazy", "exclude_tools", "strip_schema_keys") and bool(v))
                continue
            if k == "env" and isinstance(v, dict):
                entry.setdefault("env", {}).update(v)
            else:
                entry[k] = v
        if lazy:
            # 실제 서버를 mcp-lazy-proxy.py 로 감싸서 llama-server 의 10초 워밍업 제한을 우회
            wait = str(o.get("lazy_wait", 5))
            pargs = [proxy, name, "--wait", wait]
            if o.get("exclude_tools"):
                pargs += ["--exclude", ",".join(o["exclude_tools"])]
            if o.get("strip_schema_keys"):
                pargs += ["--strip", ",".join(o["strip_schema_keys"])]
            entry["args"] = pargs + ["--", entry["command"]] + entry["args"]
            entry["command"] = sys.executable
            kind += " (proxy" + (f" -{len(o.get('exclude_tools', []))}tools" if o.get("exclude_tools") else "") + (" strip" if o.get("strip_schema_keys") else "") + ")"
        out["mcpServers"][name] = entry
        print(f"  - {name:20s} {kind}" + (f"  timeout_ms={entry['timeout_ms']}" if "timeout_ms" in entry else ""))

    dst = os.path.join(HERE, "mcp.json")
    if not DRY:
        json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[mcp] {len(out['mcpServers'])}개 서버 -> {dst}" + ("  (dry-run, 미저장)" if DRY else ""))

    # WebUI 브라우저쪽 MCP 클라이언트용 목록 (게이트웨이 URL). enabled 는 기본 꺼짐 -> 필요한 것만 켜서 사용
    ui_list = []
    for name in out["mcpServers"]:
        o = dict(defaults); o.update(ov.get(name, {}))
        ui_list.append({
            "id": f"lms-{name}", "name": name, "displayName": name,
            "url": f"http://127.0.0.1:{GATEWAY_PORT}/{name}/mcp",
            "enabled": bool(o.get("ui_enabled", False)), "useProxy": False,
        })
    ui_cfg = {"mcpServers": json.dumps(ui_list, ensure_ascii=False)}
    imp = {"config": dict(ui_cfg)}
    if not DRY:
        json.dump(ui_cfg, open(os.path.join(HERE, "ui-config.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        json.dump(imp, open(os.path.join(HERE, "webui-settings-import.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[ui] WebUI MCP 서버 목록 {len(ui_list)}개 -> ui-config.json / webui-settings-import.json  (gateway port {GATEWAY_PORT}, 기본 켜짐: {[x['name'] for x in ui_list if x['enabled']] or '없음'})")


if __name__ == "__main__":
    print(f"LM Studio home: {LM}")
    if DO_MODELS:
        entries, skipped, roots = build_models()
        write_models_ini(entries, skipped, roots)
    if DO_MCP:
        build_mcp()

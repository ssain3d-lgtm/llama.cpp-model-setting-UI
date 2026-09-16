# -*- coding: utf-8 -*-
"""
llama.cpp 설정 UI 서버  (설계: docs/config-ui-design.md)

  pythonw config-ui\\server.py [--port 8092] [--open]
    --open   브라우저로 열기. 이미 떠 있으면 브라우저만 열고 종료

  - 모델별 llama-server 옵션 -> models.override.ini 편집 -> sync-lmstudio.py --models -> 라우터 /models?reload=1
  - 옵션 목록은 llama-server.exe --help 를 파싱 (llama.cpp 업데이트 시 자동 반영)
  - 모델 로드/언로드 (라우터 API), 라우터 실행 옵션(router-options.json) + 시작/중지/재시작
  - 표준 라이브러리만 사용. 127.0.0.1 전용
"""
import http.server, importlib.util, json, os, re, shutil, socket, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "llama-server.exe")
SYNC = os.path.join(ROOT, "sync-lmstudio.py")
MODELS_INI = os.path.join(ROOT, "models.ini")
OVERRIDE = os.path.join(ROOT, "models.override.ini")
ROUTER_OPTS = os.path.join(ROOT, "router-options.json")
ROUTER_BAT = os.path.join(ROOT, "start-router.bat")
LOG = os.path.join(ROOT, "config-ui.log")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def opt(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


PORT = int(opt("--port", "8092"))
APP_ID = "llama-config-ui"

if sys.stdout is None or sys.stderr is None:  # pythonw: 콘솔 없음 -> 로그 파일
    _log = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


def python_exe():
    p = os.path.join(os.path.dirname(sys.executable), "python.exe")
    return p if os.path.isfile(p) else sys.executable


TRUTHY = {"on", "enabled", "true", "1"}  # common_arg_utils::is_truthy / is_falsey 와 동일
FALSEY = {"off", "disabled", "false", "0"}


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ============================================================================ 옵션 카탈로그 (--help 파싱)
GROUP_TITLES = {
    "common params": "공통",
    "sampling params": "샘플링",
    "speculative params": "추측 디코딩",
    "example-specific params": "서버",
}
DESC_COL = 40
HIDDEN = {"help", "version", "cache-list", "completion-bash", "list-devices"}
RESERVED = {  # 라우터가 직접 정하거나(unset_reserved_args / HOST, PORT, ALIAS) 모델별로 의미 없는 키
    "host", "port", "alias", "models-dir", "models-preset", "models-max", "models-autoload",
    "api-key", "ssl-key-file", "ssl-cert-file",
}
NUMBER_HINTS = {"N", "P", "SEED", "INDEX", "SECONDS", "SIMILARITY"}
PATH_HINTS = {"FNAME", "PATH", "FILE", "JINJA_TEMPLATE_FILE"}
PRESET_ONLY = [  # common_params_add_preset_options: --help 에 안 나오는 라우터 프리셋 전용 키
    {"id": "load-on-startup", "names": ["load-on-startup"], "neg": [], "kind": "flag", "hint": "", "choices": [],
     "suggest": [], "default": "false", "help": "라우터 모드: 라우터 시작 시 이 모델을 자동 로드", "group": "router",
     "deprecated": False, "reserved": False},
    {"id": "stop-timeout", "names": ["stop-timeout"], "neg": [], "kind": "number", "hint": "SECONDS", "choices": [],
     "suggest": [], "default": "10", "help": "라우터 모드: 언로드 시 정상 종료를 이 시간(초)만큼 기다린 뒤 강제 종료",
     "group": "router", "deprecated": False, "reserved": False},
]


def _split_names(spec):
    names, rest = [], spec
    while True:
        m = re.match(r"(-{1,2}[A-Za-z0-9][\w.\-]*)(,\s*|\s+|$)", rest)
        if not m:
            break
        names.append(m.group(1))
        rest = rest[m.end():]
        if not m.group(2).startswith(","):
            break
    return names, rest.strip()


def _paren_after(text, marker):
    i = text.find(marker)
    if i < 0:
        return None
    j, depth = i + len(marker), 1
    while j < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[j], 0)
        j += 1
    return text[i + len(marker):j - 1].strip() if depth == 0 else None


def _finish(o):
    raw = re.sub(r"\s+", " ", " ".join(o.pop("_desc"))).strip()
    if "argument has been removed" in raw:
        return None
    names = o.pop("_names")
    longs = [n[2:] for n in names if n.startswith("--")]
    if not longs:
        return None
    # 이름 순서 = args 다음 args_neg. 첫 --no-xxx 앞의 짧은 이름들도 부정형
    neg_start = next((i for i, n in enumerate(names) if n.startswith("--no-")), None)
    if neg_start is not None and any(not n.startswith("--no-") for n in names[:neg_start] if n.startswith("--")):
        while neg_start > 0 and not names[neg_start - 1].startswith("--"):
            neg_start -= 1
        pos_names, neg_names = names[:neg_start], names[neg_start:]
    else:
        pos_names, neg_names = names, []
    pos_long = [n[2:] for n in pos_names if n.startswith("--")]
    oid = pos_long[0]
    default = _paren_after(raw, "(default:")
    if default is None:
        m = re.search(r"default:\s*([^\s,)]+)", raw)
        default = m.group(1) if m else ""
    # "40, 0 = disabled" -> "40",  "'auto' (detect from template)" -> "auto"  (전체 문장은 help 에 남아 있음)
    default = re.sub(r"\s*\(.*\)$", "", default)
    m = re.match(r"^([^\s,]+),\s", default)
    if m:
        default = m.group(1)
    default = default.strip().strip("'\"")
    desc = re.sub(r"\(env: [^)]*\)", "", raw)
    desc = re.sub(r"\[\((more info|card)\)\]\([^)]*\)", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    hint = o["hint"]
    kind, choices = "text", []
    if not hint:
        kind = "bool" if neg_names else "flag"
    else:
        m = (re.fullmatch(r"\[([\w|\-]+)\]", hint) or re.fullmatch(r"\{([\w,\-]+)\}", hint)
             or re.fullmatch(r"<(\w+(?:\|\w+)+)>", hint))
        am = re.search(r"allowed values:\s*(\w+(?:,\s*\w+)+)", desc)
        if m:
            kind, choices = "enum", re.split(r"[|,]", m.group(1))
        elif re.fullmatch(r"[\w\-]+(?:,[\w\-]+){2,}", hint):
            kind, choices = "list", hint.split(",")
        elif am:
            kind, choices = "enum", [c.strip() for c in am.group(1).split(",")]
        elif hint in NUMBER_HINTS or re.fullmatch(r"<[\d.]+\.\.\.[\d.]+>", hint):
            kind = "number"
        elif hint in PATH_HINTS:
            kind = "path"
    suggest = []
    if kind in ("number", "text"):
        suggest = [w for w in re.findall(r"'([\w\-]+)'", desc) if w not in ("on", "off")][:10]
    return {
        "id": oid,
        "names": [n.lstrip("-") for n in pos_names],
        "neg": [n.lstrip("-") for n in neg_names],
        "kind": kind, "hint": hint, "choices": choices, "suggest": suggest,
        "default": default, "help": desc, "group": o["group"],
        "deprecated": "DEPRECATED" in desc,
        "reserved": bool(set(longs) & RESERVED),
    }


def parse_help(text):
    opts, group, cur = [], "common", None
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        m = re.fullmatch(r"-----\s*(.+?)\s*-----", s)
        if m:
            if cur:
                opts.append(_finish(cur))
            cur, group = None, m.group(1)
            continue
        if not s:
            continue
        if line.startswith("-"):
            if cur:
                opts.append(_finish(cur))
            if len(line) > DESC_COL and line[DESC_COL - 1] == " " and line[DESC_COL] != " ":
                spec, desc = line[:DESC_COL].rstrip(), line[DESC_COL:].strip()
            else:
                spec, desc = s, ""
            names, hint = _split_names(spec)
            cur = {"_names": names, "hint": hint, "_desc": [desc], "group": group} if names else None
        elif cur:
            cur["_desc"].append(s)
    if cur:
        opts.append(_finish(cur))
    out = [o for o in opts if o and not (set(o["names"]) & HIDDEN) and not o["id"].endswith("-default")]
    for o in out:
        o["group"] = GROUP_TITLES.get(o["group"], o["group"])
    return out


_catalog = {"key": None, "data": None}
_catalog_lock = threading.Lock()


def catalog():
    st = os.stat(EXE)
    key = (st.st_mtime, st.st_size)
    with _catalog_lock:
        if _catalog["key"] != key:
            r = subprocess.run([EXE, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               creationflags=NO_WINDOW, timeout=120, cwd=ROOT)
            options = parse_help(r.stdout + "\n" + r.stderr)
            if len(options) < 50:
                raise ApiError(f"llama-server --help 파싱 실패 (옵션 {len(options)}개)", 500)
            for p in PRESET_ONLY:
                options.append(dict(p, group="라우터 전용"))
            by_id, alias = {}, {}
            for o in options:
                by_id[o["id"]] = o
                for n in o["names"] + o["neg"]:
                    alias.setdefault(n, o["id"])
            _catalog["data"] = {"options": options, "by_id": by_id, "alias": alias}
            _catalog["key"] = key
            log(f"catalog: {len(options)} options from {EXE}")
        return _catalog["data"]


# ============================================================================ 모델 데이터 (sync-lmstudio.py 재사용)
_sync_lock = threading.Lock()


def load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_lmstudio", SYNC)
    mod = importlib.util.module_from_spec(spec)
    argv = sys.argv
    sys.argv = [SYNC]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def mtime(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0


def normalize(cat, pairs):
    """[(key, value)] -> ({옵션 id: {key, value}}, [(알 수 없는 key, value)]). bool 의 값은 긍정형 기준으로 뒤집어 둠."""
    known, unknown = {}, []
    for k, v in pairs:
        oid = cat["alias"].get(k)
        if not oid:
            unknown.append([k, v])
            continue
        o = cat["by_id"][oid]
        val = str(v)
        if k in o["neg"] and val != "!":
            val = "false" if val.lower() in TRUTHY else "true"
        known[oid] = {"key": k, "value": val}
    return known, unknown


_models = {"key": None, "data": None}


def model_data():
    cat = catalog()
    key = (mtime(MODELS_INI), mtime(OVERRIDE), _catalog["key"])
    with _sync_lock:
        if _models["key"] == key:
            return _models["data"]
        sync = load_sync_module()
        entries, skipped, _roots = sync.build_models()
        ov, ov_order = sync.parse_ini(OVERRIDE)
        _glob_auto, base = sync.compose_models(entries, {})
        ini, _ = sync.parse_ini(MODELS_INI)
        glob, glob_unknown = normalize(cat, ini.get("*", []))
        models = []
        for e, sec, notes in base:
            info = e["info"]
            auto, auto_unknown = normalize(cat, [(k, sync.fmt_val(v)) for k, v in sec.items()])
            over, over_unknown = normalize(cat, ov.get(e["id"], []))
            models.append({
                "id": e["id"], "display": e["display"], "arch": info["arch"], "ctx_train": info["ctx_train"],
                "size_gb": round(os.path.getsize(e["path"]) / 1e9, 1), "mtp": info["mtp"],
                "vision": bool(e["mmproj"]), "draft": os.path.basename(e["draft"]) if e["draft"] else None,
                "file": os.path.basename(e["path"]), "extra": False, "notes": notes,
                "auto": auto, "override": over, "unknown": auto_unknown + over_unknown,
            })
        known_ids = {"*"} | {m["id"] for m in models}
        for s in ov_order:
            if s in known_ids:
                continue
            over, over_unknown = normalize(cat, ov[s])
            path = dict(ov[s]).get("model", "")
            info = {}
            try:
                info = sync.inspect_gguf(path)
            except Exception:
                pass
            models.append({
                "id": s, "display": s, "arch": info.get("arch", "?"), "ctx_train": info.get("ctx_train"),
                "size_gb": round(os.path.getsize(path) / 1e9, 1) if os.path.isfile(path) else None,
                "mtp": info.get("mtp", False), "vision": "mmproj" in dict(ov[s]), "draft": None,
                "file": os.path.basename(path) if path else "", "extra": True,
                "notes": [] if os.path.isfile(path) else ["model 파일이 없음 - 로드 실패함"],
                "auto": {}, "override": over, "unknown": over_unknown,
            })
        data = {"global": glob, "global_unknown": glob_unknown, "models": models,
                "skipped": [[os.path.basename(g), why] for g, why in skipped]}
        _models["key"], _models["data"] = key, data
        return data


# ============================================================================ models.override.ini 편집
def edit_section(text, section, updates):
    """section 의 키 줄만 수정. updates = {철자 그대로의 key: 값 | None(줄 삭제)}. 주석/다른 섹션/순서 유지."""
    nl = "\r\n" if "\r\n" in text else "\n"
    pending = {k: v for k, v in updates.items() if v is not None}
    written = set()
    out, cur, spans = [], None, []
    for line in text.splitlines():
        s = line.strip()
        item = [line, True]
        if s and s[0] not in ";#" and s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            out.append(item)
            if cur == section:
                spans.append({"hdr": item, "keys": 0, "after": item})
            continue
        if cur == section and s and s[0] not in ";#" and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                if k in pending and k not in written:
                    item[0] = f"{k} = {pending[k]}"
                    written.add(k)
                else:
                    continue
            spans[-1]["keys"] += 1
            spans[-1]["after"] = item
        out.append(item)
    rest = [f"{k} = {v}" for k, v in pending.items() if k not in written]
    if rest:
        if spans:
            sp = spans[-1]
            pos = next(i for i, it in enumerate(out) if it is sp["after"]) + 1
            out[pos:pos] = [[r, True] for r in rest]
            sp["keys"] += len(rest)
        else:
            while out and not out[-1][0].strip():
                out.pop()
            out += [["", True], [f"[{section}]", True]] + [[r, True] for r in rest]
    for sp in spans:
        if sp["keys"] == 0:
            sp["hdr"][1] = False
    kept = [line for line, keep in out if keep]
    while kept and not kept[-1].strip():
        kept.pop()
    return nl.join(kept) + nl


def check_value(o, val):
    if any(c in val for c in "\r\n"):
        raise ApiError(f"--{o['id']}: 값에 줄바꿈을 쓸 수 없어요")
    if ";" in val or "#" in val:
        extra = " (샘플러 순서는 sampler-seq 를 쓰세요)" if o["id"] == "samplers" else ""
        raise ApiError(f"--{o['id']}: 값에 ; 나 # 는 쓸 수 없어요 - llama.cpp ini 에서 주석으로 잘림{extra}")
    if not val:
        raise ApiError(f"--{o['id']}: 빈 값")
    if o["kind"] in ("bool", "flag") and val.lower() not in TRUTHY | FALSEY:
        raise ApiError(f"--{o['id']}: true / false 만 가능")
    if o["kind"] == "enum" and val not in o["choices"]:
        raise ApiError(f"--{o['id']}: {', '.join(o['choices'])} 중 하나")
    if o["kind"] == "number" and val not in o["suggest"]:
        try:
            float(val)
        except ValueError:
            raise ApiError(f"--{o['id']}: 숫자가 아님: {val}")


def read_override():
    raw = open(OVERRIDE, "rb").read() if os.path.isfile(OVERRIDE) else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig"), bom


_save_lock = threading.Lock()


def save_model(mid, changes):
    if not isinstance(changes, dict) or not changes:
        raise ApiError("변경 사항 없음")
    with _save_lock:
        return _save_model(mid, changes)


def _save_model(mid, changes):
    cat = catalog()
    data = model_data()
    m = next((x for x in data["models"] if x["id"] == mid), None)
    if not m:
        raise ApiError(f"모델 없음: {mid}", 404)
    text, bom = read_override()
    sync = load_sync_module()
    raw_sec = sync.parse_ini(OVERRIDE)[0].get(mid, [])
    updates = {}
    for oid, val in changes.items():
        o = cat["by_id"].get(oid)
        if not o:
            raise ApiError(f"알 수 없는 옵션: {oid}")
        if o["reserved"]:
            raise ApiError(f"--{oid} 는 라우터가 관리하는 옵션")
        if oid == "model" and not m["extra"]:
            raise ApiError("LM Studio 모델의 파일 경로는 바꿀 수 없어요")
        spellings = [k for k, _ in raw_sec if cat["alias"].get(k) == oid]
        base_key = m["auto"].get(oid, {}).get("key")
        key = spellings[-1] if spellings else (base_key or data["global"].get(oid, {}).get("key") or oid)
        for k in spellings:
            updates[k] = None
        if val is None:
            continue
        val = str(val).strip()
        if val == "!":
            if not base_key:
                raise ApiError(f"--{oid}: 자동 값이 없어서 제거할 게 없어요")
            if oid == "model":
                raise ApiError("model 은 제거할 수 없어요")
            updates[base_key] = "!"
            continue
        check_value(o, val)
        if o["kind"] in ("bool", "flag"):
            on = val.lower() in TRUTHY
            if key in o["neg"]:
                on = not on
            val = "true" if on else "false"
        updates[key] = val
    new_text = edit_section(text, mid, updates)
    if new_text == text:
        return {"changed": False}
    bak = OVERRIDE + ".bak"
    if os.path.isfile(OVERRIDE) and not os.path.isfile(bak):
        shutil.copy2(OVERRIDE, bak)
    with open(OVERRIDE, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        f.write(new_text)
    r = run_sync()
    if r.returncode != 0:
        with open(OVERRIDE, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
            f.write(text)
        run_sync()
        raise ApiError("sync-lmstudio.py 실패 - 이전 설정으로 되돌림:\n" + (r.stdout + r.stderr)[-1500:], 500)
    result = {"changed": True, "sync": r.stdout[-3000:], "reloaded": False, "unloaded": []}
    before = router_models()
    if before is not None:
        try:
            http_json("GET", "/models?reload=1", timeout=180)
            result["reloaded"] = True
            after = router_models() or {}
            result["unloaded"] = [k for k, v in before.items()
                                  if v["status"] in ("loaded", "loading", "sleeping") and after.get(k, {}).get("status") == "unloaded"]
        except Exception as e:
            result["reload_error"] = str(e)
    log(f"saved {mid}: {updates}  reloaded={result['reloaded']} unloaded={result['unloaded']}")
    return result


def run_sync():
    with _sync_lock:
        return subprocess.run([python_exe(), SYNC, "--models"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=NO_WINDOW, cwd=ROOT, timeout=300)


# ============================================================================ 라우터
ROUTER_FIELDS = [
    {"name": "Port", "type": "int", "default": 8080, "min": 1024, "max": 65535, "label": "라우터 포트",
     "help": "WebUI / API 주소의 포트. 바꾸면 기존 'llama.cpp WebUI' 바로가기(8080 고정)와 ComfyUI 노드 주소도 바꿔야 함"},
    {"name": "ModelsMax", "type": "int", "default": 1, "min": 0, "max": 16, "label": "동시에 올릴 모델 수",
     "help": "VRAM 에 동시에 상주할 모델 수. 넘치면 오래 안 쓴 모델부터 내림. 0 = 무제한"},
    {"name": "Build", "type": "enum", "choices": ["official", "fastmtp"], "default": "official", "label": "빌드",
     "help": "official = C:\\AI\\llama.cpp,  fastmtp = FastMTP 패치 빌드(C:\\AI\\llama.cpp-fastmtp, 실험용)"},
    {"name": "McpMode", "type": "enum", "choices": ["browser", "server", "off"], "default": "browser", "label": "MCP 모드",
     "help": "browser = WebUI 에서 서버별로 켜고 끔(게이트웨이),  server = llama-server 가 전부 직접 띄움,  off = MCP 없음"},
    {"name": "GatewayPort", "type": "int", "default": 8091, "min": 1024, "max": 65535, "label": "MCP 게이트웨이 포트",
     "help": "browser 모드에서 MCP 서버를 HTTP 로 노출하는 포트"},
    {"name": "GatewayIdle", "type": "int", "default": 600, "min": 0, "max": 86400, "label": "MCP 서버 유휴 종료(초)",
     "help": "이 시간 동안 안 쓴 MCP 서버 프로세스를 종료"},
    {"name": "WatchInterval", "type": "int", "default": 15, "min": 2, "max": 3600, "label": "모델 폴더 감시 간격(초)",
     "help": "LM Studio 모델 폴더/설정 변경을 확인하는 주기"},
    {"name": "NoWatch", "type": "bool", "default": False, "label": "모델 폴더 감시 끄기",
     "help": "켜면 새 모델을 받아도 라우터 재시작 전까지 목록에 안 뜸"},
    {"name": "NoSync", "type": "bool", "default": False, "label": "시작 시 동기화 건너뛰기",
     "help": "켜면 라우터 시작 때 sync-lmstudio.py 를 실행하지 않음(기존 models.ini 사용)"},
]


def load_router_opts():
    try:
        d = json.load(open(ROUTER_OPTS, encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_active_port = {"v": None}  # 포트를 바꿔 저장했지만 아직 재시작 전이면 실행 중인 라우터의 포트


def router_port():
    if _active_port["v"]:
        return _active_port["v"]
    return saved_port()


def saved_port():
    try:
        return int(load_router_opts().get("Port", 8080))
    except (TypeError, ValueError):
        return 8080


def save_router_opts(values):
    if not isinstance(values, dict):
        raise ApiError("잘못된 요청")
    out = {}
    for f in ROUTER_FIELDS:
        if f["name"] not in values:
            continue
        v = values[f["name"]]
        if f["type"] == "int":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ApiError(f"{f['label']}: 정수가 아님")
            if not f["min"] <= v <= f["max"]:
                raise ApiError(f"{f['label']}: {f['min']} ~ {f['max']}")
        elif f["type"] == "enum":
            if v not in f["choices"]:
                raise ApiError(f"{f['label']}: {', '.join(f['choices'])} 중 하나")
        else:
            v = bool(v)
        if v != f["default"]:
            out[f["name"]] = v
    new_port = out.get("Port", 8080)
    if new_port != router_port() and router_up():
        _active_port["v"] = router_port()
    with open(ROUTER_OPTS, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return out


def http_json(method, path, body=None, timeout=3):
    url = f"http://127.0.0.1:{router_port()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message") or str(e)
        except Exception:
            msg = str(e)
        raise ApiError(f"라우터: {msg}", 502)


def port_open(port):
    """Windows 는 닫힌 localhost 포트 연결이 거부되기까지 ~2초 걸림 -> 짧은 타임아웃으로 먼저 확인"""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def router_models():
    """{id: {status, failed, exit_code, args}} 또는 라우터가 꺼져 있으면 None"""
    if not port_open(router_port()):
        return None
    try:
        d = http_json("GET", "/models", timeout=2)
    except Exception:
        return None
    out = {}
    for x in d.get("data", []):
        st = x.get("status") or {}
        out[x["id"]] = {"status": st.get("value", "unknown"), "failed": bool(st.get("failed")),
                        "exit_code": st.get("exit_code"), "args": st.get("args", [])}
    return out


_vram = {"t": 0, "v": None}


def vram():
    if time.time() - _vram["t"] < 2:
        return _vram["v"]
    v = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        name, used, total = [s.strip() for s in r.stdout.strip().splitlines()[0].split(",")]
        v = {"name": name, "used_mb": int(used), "total_mb": int(total)}
    except Exception:
        pass
    _vram.update(t=time.time(), v=v)
    return v


def processes():
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe' or Name='powershell.exe' or Name='pwsh.exe' or Name='cmd.exe'\" "
          "| Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30, creationflags=NO_WINDOW)
    d = json.loads(r.stdout or "[]")
    return {p["ProcessId"]: p for p in ([d] if isinstance(d, dict) else d)}


def find_router():
    """이 폴더의 models.ini 로 띄운 라우터(llama-server --models-preset) + 트리 루트(start-router.bat/.ps1)"""
    procs = processes()
    ini = MODELS_INI.lower().replace("/", "\\")
    for p in procs.values():
        cmd = p.get("CommandLine") or ""
        if p["Name"].lower() != "llama-server.exe" or ini not in cmd.lower().replace("/", "\\"):
            continue
        m = re.search(r"--port\s+(\d+)", cmd)
        port = int(m.group(1)) if m else 8080
        root = p
        while True:
            parent = procs.get(root["ParentProcessId"])
            pcmd = ((parent or {}).get("CommandLine") or "").lower()
            if parent and ("start-router.ps1" in pcmd or "start-router.bat" in pcmd):
                root = parent
            else:
                break
        mm = re.search(r"--models-max\s+(\d+)", cmd)
        running = {
            "Port": port,
            "ModelsMax": int(mm.group(1)) if mm else 4,
            "Build": "fastmtp" if "llama.cpp-fastmtp" in cmd.lower() else "official",
            "McpMode": "server" if "--mcp-servers-config" in cmd else ("browser" if "--ui-config-file" in cmd else "off"),
        }
        return {"pid": p["ProcessId"], "root_pid": root["ProcessId"], "root_name": root["Name"],
                "cmdline": cmd, "running": running}
    return None


def router_up():
    if not port_open(router_port()):
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{router_port()}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def stop_router():
    info = find_router()
    if not info:
        if router_up():
            raise ApiError("라우터가 응답하지만 프로세스를 못 찾았어요 (다른 방법으로 띄운 것 같음) - 직접 종료해 주세요", 409)
        return {"stopped": False, "msg": "실행 중이 아님"}
    subprocess.run(["taskkill", "/PID", str(info["root_pid"]), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    for _ in range(40):
        if not router_up():
            break
        time.sleep(0.25)
    _active_port["v"] = None
    log(f"router stopped (root pid {info['root_pid']} {info['root_name']})")
    return {"stopped": True, "pid": info["root_pid"]}


def start_router():
    if router_up():
        return {"started": False, "msg": "이미 실행 중"}
    subprocess.Popen(f'cmd.exe /c start "llama.cpp router" "{ROUTER_BAT}"', cwd=ROOT, creationflags=NO_WINDOW)
    log("router start requested")
    return {"started": True}


# ============================================================================ HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "llama-config-ui"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                log(f"GET {path}  host={self.headers.get('Host')}")
                return self._send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
            if path == "/api/ping":
                return self._send(200, {"app": APP_ID})
            if path == "/api/state":
                cat = catalog()
                data = model_data()
                return self._send(200, {
                    "options": cat["options"], "global": data["global"], "global_unknown": data["global_unknown"],
                    "models": data["models"], "skipped": data["skipped"],
                    "router_fields": ROUTER_FIELDS, "router_options": load_router_opts(),
                    "paths": {"override": OVERRIDE, "models_ini": MODELS_INI, "root": ROOT},
                })
            if path == "/api/live":
                return self._send(200, {"router": {"port": router_port(), "models": router_models()}, "vram": vram()})
            if path == "/api/router/info":
                return self._send(200, {"process": find_router(), "up": router_up(), "options": load_router_opts()})
            return self._send(404, {"error": "not found"})
        except ApiError as e:
            return self._send(e.status, {"error": str(e)})
        except Exception as e:
            log(f"GET {path} failed: {e!r}")
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        origin = self.headers.get("Origin")
        log(f"POST {path}  host={self.headers.get('Host')} origin={origin}")
        try:  # 거절할 때도 본문을 먼저 읽음 (안 읽고 닫으면 Windows 가 RST 를 보내 브라우저엔 'Failed to fetch' 로만 보임)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
        except (ValueError, OSError):
            raw = b""
        if not self._host_ok():
            log(f"  rejected: host")
            return self._send(403, {"error": "forbidden host"})
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            log(f"  rejected: origin")
            return self._send(403, {"error": "forbidden origin"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            log(f"  rejected: content-type {self.headers.get('Content-Type')}")
            return self._send(415, {"error": "application/json 필요"})
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            if path == "/api/model/save":
                return self._send(200, save_model(body.get("id"), body.get("changes")))
            if path in ("/api/model/load", "/api/model/unload"):
                act = path.rsplit("/", 1)[1]
                if router_models() is None:
                    raise ApiError("라우터가 꺼져 있어요", 409)
                return self._send(200, http_json("POST", f"/models/{act}", {"model": body.get("id")}, timeout=30))
            if path == "/api/router/options":
                return self._send(200, {"options": save_router_opts(body.get("options"))})
            if path == "/api/router/start":
                return self._send(200, start_router())
            if path == "/api/router/stop":
                return self._send(200, stop_router())
            if path == "/api/router/restart":
                stopped = stop_router()
                return self._send(200, dict(start_router(), stopped=stopped.get("stopped")))
            if path == "/api/shutdown":
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self._send(404, {"error": "not found"})
        except ApiError as e:
            return self._send(e.status, {"error": str(e)})
        except Exception as e:
            log(f"POST {path} failed: {e!r}")
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def already_running():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except Exception:
        return False


def main():
    url = f"http://127.0.0.1:{PORT}/"
    if already_running():
        if "--open" in sys.argv:
            webbrowser.open(url)
        return
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        log(f"port {PORT} bind failed: {e}")
        sys.exit(1)
    threading.Thread(target=lambda: (catalog(), model_data()), daemon=True).start()  # 첫 화면 빠르게
    log(f"config-ui listening on {url}")
    if "--open" in sys.argv:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    httpd.serve_forever()


if __name__ == "__main__":
    main()

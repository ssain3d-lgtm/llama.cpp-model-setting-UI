# -*- coding: utf-8 -*-
"""
mcp-lazy-proxy.py  <name> [--wait N] [--exclude t1,t2] [--strip k1,k2] -- <command> [args...]

  --exclude  tools/list 에서 제외할 툴 이름 (쉼표 구분, fnmatch 와일드카드 가능)
  --strip    툴 inputSchema 에서 재귀적으로 제거할 키 (예: pattern,format)
             -> llama.cpp 의 JSON-schema→GBNF 변환기가 못 읽는 정규식 때문에 "failed to parse grammar" 가 날 때 사용

llama-server 는 시작 시 MCP 서버마다 10초 안에 tools/list 응답을 받지 못하면
그 세션 내내 해당 서버의 툴을 0개로 둡니다 (재탐색 없음).
이 프록시는 실제 MCP 서버(stdio)를 즉시 백그라운드로 띄우고,
  - initialize / tools/list 는 실제 서버가 N초(기본 5초, 첫 요청 기준) 안에 준비되면 그대로 전달,
    아니면 마지막 성공 때 저장해 둔 캐시(mcp-cache/<name>.json)로 즉시 응답
  - tools/call 등 나머지 요청은 실제 서버가 준비될 때까지 기다렸다가 전달
합니다. 캐시는 실제 서버가 응답할 때마다 갱신됩니다.
"""
import json, os, subprocess, sys, threading, time

argv = sys.argv[1:]
if "--" not in argv or len(argv) < 3:
    sys.stderr.write(__doc__)
    sys.exit(2)
sep = argv.index("--")
head, CHILD = argv[:sep], argv[sep + 1:]
NAME = head[0]
WAIT = 5.0
if "--wait" in head:
    WAIT = float(head[head.index("--wait") + 1])
READY_MAX = float(os.environ.get("MCP_LAZY_READY_MAX", "600"))
EXCLUDE = [x for x in (head[head.index("--exclude") + 1].split(",") if "--exclude" in head else []) if x]
STRIP = [x for x in (head[head.index("--strip") + 1].split(",") if "--strip" in head else []) if x]
import fnmatch


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in STRIP}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def filter_tools(result):
    """tools/list 결과에 --exclude / --strip 적용"""
    if not (EXCLUDE or STRIP) or not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return result
    out = []
    for t in result["tools"]:
        n = t.get("name", "")
        if any(fnmatch.fnmatch(n, pat) for pat in EXCLUDE):
            continue
        if STRIP and "inputSchema" in t:
            t = dict(t); t["inputSchema"] = _strip(t["inputSchema"])
        out.append(t)
    r = dict(result); r["tools"] = out
    return r

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "mcp-cache")
CACHE = os.path.join(CACHE_DIR, NAME + ".json")
INIT_ID = "__lazy_init__"
TOOLS_ID = "__lazy_tools__"

out_lock = threading.Lock()
stdout = sys.stdout.buffer
stdin = sys.stdin.buffer


def log(msg):
    sys.stderr.write(f"[mcp-lazy-proxy:{NAME}] {msg}\n")
    sys.stderr.flush()


def send_client(obj):
    with out_lock:
        stdout.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        stdout.flush()


def load_cache():
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return {}


def save_cache(c):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        json.dump(c, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"cache write failed: {e}")


cache = load_cache()
ready = threading.Event()
dead = threading.Event()
pending = set()          # ids forwarded to child, awaiting reply
pending_lock = threading.Lock()
child_init_result = None
child_lock = threading.Lock()

try:
    child = subprocess.Popen(
        CHILD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
except Exception as e:
    log(f"failed to spawn child: {e}")
    child = None
    dead.set()


def send_child(obj):
    if child is None or dead.is_set():
        return False
    try:
        with child_lock:
            child.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            child.stdin.flush()
        return True
    except Exception as e:
        log(f"child write failed: {e}")
        return False


def fail_pending(reason):
    with pending_lock:
        ids = list(pending)
        pending.clear()
    for i in ids:
        send_client({"jsonrpc": "2.0", "id": i, "error": {"code": -32603, "message": f"{NAME}: {reason}"}})


def child_reader():
    global child_init_result
    t0 = time.time()
    try:
        for raw in child.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            mid = m.get("id")
            if mid == INIT_ID:
                if "result" in m:
                    child_init_result = m["result"]
                    cache["init"] = m["result"]
                    send_child({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    ready.set()
                    log(f"child ready in {time.time() - t0:.1f}s")
                    send_child({"jsonrpc": "2.0", "id": TOOLS_ID, "method": "tools/list", "params": {}})
                else:
                    log(f"child initialize error: {m.get('error')}")
                    dead.set()
                continue
            if mid == TOOLS_ID:
                if "result" in m:
                    cache["tools"] = filter_tools(m["result"])
                    save_cache(cache)
                    log(f"tools cached: {len(m['result'].get('tools', []))}")
                continue
            if mid is not None and ("result" in m or "error" in m):
                with pending_lock:
                    pending.discard(mid)
                # 실제 tools/list 응답이면 캐시도 갱신
                if "result" in m and isinstance(m["result"], dict) and "tools" in m["result"]:
                    m = dict(m); m["result"] = filter_tools(m["result"])
                    cache["tools"] = m["result"]
                    save_cache(cache)
            send_client(m)  # responses and server->client notifications/requests
    except Exception as e:
        log(f"child reader error: {e}")
    finally:
        dead.set()
        ready.clear()
        log(f"child exited (code={child.poll()})")
        fail_pending("MCP server process exited")


if child is not None:
    threading.Thread(target=child_reader, daemon=True).start()
    send_child({"jsonrpc": "2.0", "id": INIT_ID, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "llama-server via mcp-lazy-proxy", "version": "1"}}})


first_msg_at = None


def wait_ready(timeout):
    """timeout 이 WAIT 이면 '첫 클라이언트 메시지 + WAIT' 공용 데드라인을 쓴다
    (initialize 와 tools/list 가 각각 WAIT 만큼 기다려 합계가 10초를 넘는 일 방지)"""
    if timeout == WAIT and first_msg_at is not None:
        end = first_msg_at + WAIT
    else:
        end = time.time() + timeout
    while time.time() < end:
        if ready.is_set():
            return True
        if dead.is_set():
            return False
        time.sleep(0.02)
    return ready.is_set()


def forward(m):
    mid = m.get("id")
    if mid is not None and "method" in m:
        with pending_lock:
            pending.add(mid)
    if not send_child(m):
        if mid is not None and "method" in m:
            with pending_lock:
                pending.discard(mid)
            send_client({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{NAME}: MCP server not running"}})


def handle(m):
    method = m.get("method")
    mid = m.get("id")
    if method is None:
        forward(m)  # client's response to a server-initiated request
        return
    if method == "initialize":
        if wait_ready(WAIT) and child_init_result is not None:
            res = dict(child_init_result)
        elif cache.get("init"):
            res = dict(cache["init"])
            log("initialize answered from cache")
        else:
            res = {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": NAME, "version": "lazy"}}
            log("initialize answered synthetically (no cache yet)")
        pv = (m.get("params") or {}).get("protocolVersion")
        if pv:
            res["protocolVersion"] = pv
        send_client({"jsonrpc": "2.0", "id": mid, "result": res})
        return
    if method == "notifications/initialized":
        return  # proxy already did this with the child
    if method == "ping":
        send_client({"jsonrpc": "2.0", "id": mid, "result": {}})
        return
    if method == "tools/list":
        if wait_ready(WAIT):
            forward(m)
        elif cache.get("tools"):
            log(f"tools/list answered from cache ({len(cache['tools'].get('tools', []))} tools)")
            send_client({"jsonrpc": "2.0", "id": mid, "result": filter_tools(cache["tools"])})
        else:
            log("tools/list: no cache, waiting for child")
            if wait_ready(READY_MAX):
                forward(m)
            else:
                send_client({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{NAME}: MCP server failed to start"}})
        return
    # tools/call, resources/*, prompts/* ...
    if wait_ready(READY_MAX):
        forward(m)
    else:
        send_client({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{NAME}: MCP server failed to start"}})


def main():
    global first_msg_at
    try:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            if first_msg_at is None:
                first_msg_at = time.time()
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("method") in ("initialize", "tools/list", "ping", "notifications/initialized") or "method" not in m:
                handle(m)
            else:
                threading.Thread(target=handle, args=(m,), daemon=True).start()
    finally:
        if child is not None:
            try:
                child.stdin.close()
            except Exception:
                pass
            for _ in range(30):
                if child.poll() is not None:
                    break
                time.sleep(0.1)
            if child.poll() is None:
                child.kill()


if __name__ == "__main__":
    main()

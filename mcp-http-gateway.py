# -*- coding: utf-8 -*-
"""
mcp-http-gateway.py  [--config mcp.json] [--port 8091] [--idle 600] [--parent-pid N]

mcp.json 의 stdio MCP 서버들을 MCP Streamable-HTTP 엔드포인트로 노출합니다.
  http://127.0.0.1:8091/<서버이름>/mcp
  http://127.0.0.1:8091/            서버 목록 / 상태 (JSON)

llama.cpp WebUI 의 "MCP Servers" (브라우저 쪽 MCP 클라이언트) 에 위 URL 을 등록하면
LM Studio 처럼 서버 단위로 켜고 끄고, 대화별로도 따로 고를 수 있습니다.
  - 서버 프로세스는 첫 요청 때 기동, --idle 초 동안 요청 없으면 종료 (다음 요청 때 자동 재기동)
  - --parent-pid 로 준 프로세스가 죽으면 게이트웨이도 종료 (start-router.ps1 이 넘겨줌)
"""
import json, os, subprocess, sys, threading, time, uuid, itertools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]


def opt(name, default):
    return argv[argv.index(name) + 1] if name in argv else default


CONFIG = opt("--config", os.path.join(HERE, "mcp.json"))
PORT = int(opt("--port", "8091"))
IDLE = float(opt("--idle", "600"))
PARENT = int(opt("--parent-pid", "0"))
READY_TIMEOUT = 120.0
CALL_TIMEOUT = 900.0

LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
        sys.stderr.flush()


class StdioServer:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.lock = threading.Lock()      # spawn / state
        self.wlock = threading.Lock()     # child stdin
        self.proc = None
        self.ready = threading.Event()
        self.init_result = None
        self.session_id = uuid.uuid4().hex
        self.pending = {}                 # gateway id -> {"ev": Event, "msg": dict}
        self.plock = threading.Lock()
        self.ids = itertools.count(1)
        self.last_used = time.time()
        self.spawned_at = None
        self.exit_code = None

    # ---------------------------------------------------------------- lifecycle
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure(self):
        with self.lock:
            if self.alive() and self.ready.is_set():
                return
            if not self.alive():
                self._spawn()
        if not self.ready.wait(READY_TIMEOUT):
            raise RuntimeError(f"{self.name}: MCP server did not initialize within {READY_TIMEOUT:.0f}s")

    def _spawn(self):
        env = dict(os.environ)
        env.update(self.cfg.get("env") or {})
        cmd = [self.cfg["command"]] + list(self.cfg.get("args") or [])
        log(f"[{self.name}] spawning: {' '.join(cmd)[:160]}")
        self.ready.clear()
        self.init_result = None
        self.session_id = uuid.uuid4().hex
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=self.cfg.get("cwd") or None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.spawned_at = time.time()
        self.last_used = self.spawned_at   # 기동 중 idle reaper 에 죽지 않도록 갱신
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()
        self._send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mcp-http-gateway", "version": "1"}}})

    def stop(self, reason=""):
        with self.lock:
            p = self.proc
            self.proc = None
            self.ready.clear()
        if p is not None and p.poll() is None:
            log(f"[{self.name}] stopping ({reason})")
            try:
                p.stdin.close()
            except Exception:
                pass
            for _ in range(30):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if p.poll() is None:
                p.kill()
        self._fail_all("MCP server stopped")

    def _fail_all(self, reason):
        with self.plock:
            items = list(self.pending.items())
            self.pending.clear()
        for _, slot in items:
            slot["msg"] = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"{self.name}: {reason}"}}
            slot["ev"].set()

    # ---------------------------------------------------------------- io
    def _send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.wlock:
            if not self.alive():
                raise RuntimeError(f"{self.name}: MCP server not running")
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _stderr(self, proc):
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log(f"[{self.name}] {line[:300]}")

    def _reader(self, proc):
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            mid = m.get("id")
            if mid == 0 and "method" not in m:            # our initialize
                if "result" in m:
                    self.init_result = m["result"]
                    try:
                        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    except Exception:
                        pass
                    self.ready.set()
                    log(f"[{self.name}] ready in {time.time() - self.spawned_at:.1f}s")
                else:
                    log(f"[{self.name}] initialize failed: {m.get('error')}")
                continue
            if mid is not None and "method" in m:
                # server -> client request (sampling, roots, elicitation): 브라우저까지 전달할 스트림이 없으므로 거절
                try:
                    self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not supported through gateway"}})
                except Exception:
                    pass
                continue
            if mid is None:
                continue  # 서버 알림은 무시
            with self.plock:
                slot = self.pending.pop(mid, None)
            if slot is not None:
                slot["msg"] = m
                slot["ev"].set()
        code = proc.poll()
        self.exit_code = code
        log(f"[{self.name}] exited (code={code})")
        with self.lock:
            if self.proc is proc:
                self.proc = None
                self.ready.clear()
        self._fail_all(f"MCP server exited (code={code})")

    # ---------------------------------------------------------------- rpc
    def request(self, method, params, timeout=CALL_TIMEOUT):
        self.ensure()
        self.last_used = time.time()
        gid = next(self.ids)
        slot = {"ev": threading.Event(), "msg": None}
        with self.plock:
            self.pending[gid] = slot
        req = {"jsonrpc": "2.0", "id": gid, "method": method}
        if params is not None:
            req["params"] = params
        self._send(req)
        if not slot["ev"].wait(timeout):
            with self.plock:
                self.pending.pop(gid, None)
            return {"error": {"code": -32001, "message": f"{self.name}: timeout after {timeout:.0f}s"}}
        self.last_used = time.time()
        m = slot["msg"]
        return {"result": m["result"]} if "result" in m else {"error": m.get("error")}

    def notify(self, method, params):
        self.ensure()
        self.last_used = time.time()
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def status(self):
        return {
            "running": self.alive(), "ready": self.ready.is_set(),
            "idle_seconds": round(time.time() - self.last_used) if self.alive() else None,
            "server_info": (self.init_result or {}).get("serverInfo"),
        }


# ---------------------------------------------------------------------------- load config
try:
    CFG = json.load(open(CONFIG, encoding="utf-8"))["mcpServers"]
except Exception as e:
    sys.stderr.write(f"config load failed: {CONFIG}: {e}\n")
    sys.exit(2)
SERVERS = {n: StdioServer(n, c) for n, c in CFG.items() if c.get("command")}


def idle_reaper():
    while True:
        time.sleep(5)
        for s in SERVERS.values():
            if s.alive() and s.ready.is_set() and IDLE > 0 and time.time() - s.last_used > IDLE:
                s.stop(f"idle {IDLE:.0f}s")


def parent_watch(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        log(f"parent pid {pid} not found; exiting")
        os._exit(0)
    k32.WaitForSingleObject(h, 0xFFFFFFFF)
    log(f"parent pid {pid} exited; shutting down")
    for s in SERVERS.values():
        s.stop("gateway shutdown")
    os._exit(0)


# ---------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-http-gateway/1"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization, Mcp-Session-Id, MCP-Protocol-Version, Last-Event-ID")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id, MCP-Protocol-Version")
        self.send_header("Access-Control-Max-Age", "86400")

    def _reply(self, code, body=None, extra=None):
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _server(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) == 2 and parts[1] == "mcp" and parts[0] in SERVERS:
            return SERVERS[parts[0]]
        return None

    def do_OPTIONS(self):
        self._reply(204)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/health"):
            self._reply(200, {
                "port": PORT, "idle_seconds": IDLE,
                "servers": {n: dict(url=f"http://127.0.0.1:{PORT}/{n}/mcp", **s.status()) for n, s in SERVERS.items()},
            })
            return
        if self._server():
            self._reply(405, {"error": "SSE stream not supported; use POST"})  # allowed by spec
            return
        self._reply(404, {"error": "unknown server", "servers": list(SERVERS)})

    def do_DELETE(self):
        s = self._server()
        if not s:
            self._reply(404, {"error": "unknown server"})
            return
        self._reply(200, {})

    def do_POST(self):
        s = self._server()
        if not s:
            self._reply(404, {"error": "unknown server", "servers": list(SERVERS)})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
        except Exception as e:
            self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}})
            return
        batch = isinstance(body, list)
        msgs = body if batch else [body]
        out = []
        try:
            for m in msgs:
                r = self._handle(s, m)
                if r is not None:
                    out.append(r)
        except Exception as e:
            log(f"[{s.name}] error: {e}")
            ids = [m.get("id") for m in msgs if isinstance(m, dict) and m.get("id") is not None and "method" in m]
            err = {"code": -32603, "message": str(e)}
            out = [{"jsonrpc": "2.0", "id": i, "error": err} for i in ids] or [{"jsonrpc": "2.0", "id": None, "error": err}]
        hdr = {"Mcp-Session-Id": s.session_id}
        if not out:
            self._reply(202, None, hdr)
        else:
            self._reply(200, out if batch else out[0], hdr)

    def _handle(self, s, m):
        if not isinstance(m, dict):
            return None
        method = m.get("method")
        mid = m.get("id")
        if method is None:
            return None  # client response to a server request: not applicable
        if mid is None:   # notification
            if method == "notifications/initialized":
                s.ensure()
            else:
                try:
                    s.notify(method, m.get("params"))
                except Exception as e:
                    log(f"[{s.name}] notify failed: {e}")
            return None
        if method == "initialize":
            s.ensure()
            res = dict(s.init_result or {"capabilities": {"tools": {}}, "serverInfo": {"name": s.name, "version": "gateway"}})
            pv = (m.get("params") or {}).get("protocolVersion")
            if pv:
                res["protocolVersion"] = pv
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        r = s.request(method, m.get("params"))
        return {"jsonrpc": "2.0", "id": mid, **r}


if __name__ == "__main__":
    threading.Thread(target=idle_reaper, daemon=True).start()
    if PARENT:
        threading.Thread(target=parent_watch, args=(PARENT,), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.daemon_threads = True
    log(f"mcp-http-gateway on http://127.0.0.1:{PORT}/  servers={len(SERVERS)} idle={IDLE:.0f}s parent={PARENT or '-'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in SERVERS.values():
            s.stop("shutdown")

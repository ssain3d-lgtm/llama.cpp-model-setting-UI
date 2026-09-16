# -*- coding: utf-8 -*-
"""
watch-lmstudio.py  [--port 8080] [--interval 15] [--settle 30] [--parent-pid N]

LM Studio 모델 폴더를 주기적으로 확인해서 모델이 추가/삭제/변경되면
  1) sync-lmstudio.py --models 로 models.ini 재생성
  2) 내용이 실제로 바뀌었으면 http://127.0.0.1:<port>/models?reload=1 로 라우터에 반영 (재시작 불필요)
라우터 reload 동작: 실행 중인 모델은 프리셋이 바뀌었거나 사라졌을 때만 언로드, 나머지는 그대로 유지.

감시 대상: LM Studio models 폴더(+ settings.json downloadsFolder) + model-folders.json 의 폴더에 있는 *.gguf 크기/수정시각,
          LM Studio 모델 인덱스(model-index-cache.json), 모델별 로드 설정, settings.json, models.override.ini, model-folders.json
다운로드 중인 파일은 두 주기 연속 변화가 없고 마지막 수정 후 --settle 초가 지나야 반영.
--parent-pid 로 준 프로세스(start-router.ps1)가 죽으면 같이 종료.
"""
import json, os, subprocess, sys, threading, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def opt(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


PORT = int(opt("--port", "8080"))
INTERVAL = float(opt("--interval", "15"))
SETTLE = float(opt("--settle", "30"))
PARENT = int(opt("--parent-pid", "0"))


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


def lm_home():
    p = os.path.expanduser("~/.lmstudio-home-pointer")
    if os.path.isfile(p):
        v = open(p, encoding="utf-8").read().strip()
        if v and os.path.isdir(v):
            return v
    return os.path.expanduser("~/.lmstudio")


def load_folders():
    """model-folders.json (sync-lmstudio.py 와 같은 형식) -> (LM Studio 포함 여부, [모델 폴더])"""
    try:
        d = json.load(open(os.path.join(HERE, "model-folders.json"), encoding="utf-8-sig"))
    except Exception:
        return True, []
    folders = [os.path.abspath(os.path.expanduser(f)) for f in d.get("folders", []) if isinstance(f, str) and f.strip()]
    return d.get("lmstudio", True) is not False, folders


def model_roots(lm):
    use_lm, folders = load_folders()
    roots = []
    if use_lm:
        roots.append(os.path.join(lm, "models"))
        try:
            dl = json.load(open(os.path.join(lm, "settings.json"), encoding="utf-8")).get("downloadsFolder")
        except Exception:
            dl = None
        if dl and os.path.isdir(dl) and not os.path.normcase(os.path.abspath(dl)).startswith(os.path.normcase(os.path.abspath(roots[0]))):
            roots.append(dl)
    return roots + [f for f in folders if os.path.isdir(f)]


def fingerprint():
    """(변화 감지용 튜플, 가장 최근 GGUF 수정시각)"""
    lm = lm_home()
    items, newest = [], 0.0

    def add(p, with_size):
        nonlocal newest
        try:
            st = os.stat(p)
        except OSError:
            return
        items.append((p.lower(), st.st_size if with_size else 0, int(st.st_mtime)))
        if with_size:
            newest = max(newest, st.st_mtime)

    for root in model_roots(lm):
        for dp, _, fn in os.walk(root):
            for f in fn:
                if f.lower().endswith(".gguf"):
                    add(os.path.join(dp, f), True)
    for p in (os.path.join(lm, "settings.json"),
              os.path.join(lm, ".internal", "model-index-cache.json"),
              os.path.join(HERE, "models.override.ini"),
              os.path.join(HERE, "model-folders.json")):
        add(p, False)
    for dp, _, fn in os.walk(os.path.join(lm, ".internal", "user-concrete-model-default-config")):
        for f in fn:
            add(os.path.join(dp, f), False)
    return tuple(sorted(items)), newest


def read_bytes(p):
    try:
        return open(p, "rb").read()
    except OSError:
        return b""


def section_ids(data):
    out = set()
    for line in data.decode("utf-8", "replace").splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]") and s != "[*]":
            out.add(s[1:-1])
    return out


def sync_and_reload():
    ini = os.path.join(HERE, "models.ini")
    before = read_bytes(ini)
    r = subprocess.run([sys.executable, os.path.join(HERE, "sync-lmstudio.py"), "--models"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        log(f"sync failed (exit {r.returncode}): {(r.stdout + r.stderr)[-800:]}")
        return
    after = read_bytes(ini)
    if after == before:
        log("sync: models.ini unchanged")
        return
    added, removed = section_ids(after) - section_ids(before), section_ids(before) - section_ids(after)
    log(f"models.ini changed  added={sorted(added)}  removed={sorted(removed)}")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/models?reload=1", timeout=180) as resp:
            n = len(json.load(resp).get("data", []))
        log(f"router reloaded: {n} models")
    except Exception as e:
        log(f"router reload failed (router not running?): {e}")


def parent_watch(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        log(f"parent pid {pid} not found; exiting")
        os._exit(0)
    k32.WaitForSingleObject(h, 0xFFFFFFFF)
    log(f"parent pid {pid} exited; shutting down")
    os._exit(0)


def main():
    if PARENT:
        threading.Thread(target=parent_watch, args=(PARENT,), daemon=True).start()
    synced, _ = fingerprint()  # start-router.ps1 가 기동 직전에 이미 동기화함 -> 현재 상태를 기준으로
    pending = None
    log(f"watching LM Studio models: {model_roots(lm_home())}  interval={INTERVAL:.0f}s settle={SETTLE:.0f}s port={PORT}")
    while True:
        time.sleep(INTERVAL)
        try:
            fp, newest = fingerprint()
        except Exception as e:
            log(f"scan error: {e}")
            continue
        if fp == synced:
            pending = None
            continue
        if fp != pending:  # 변화 감지 -> 다음 주기에도 같으면(다운로드/쓰기 끝) 반영
            if pending is None:
                log("change detected, waiting to settle")
            pending = fp
            continue
        if time.time() - newest < SETTLE:
            continue
        sync_and_reload()
        synced, pending = fp, None


if __name__ == "__main__":
    main()

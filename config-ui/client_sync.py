# -*- coding: utf-8 -*-
"""클라이언트(Qwen Code, Hermes Agent)의 모델 설정을 로컬 추론 서버 설정에 맞춘다.

두 클라이언트 모두 서버(/v1/models)에서 컨텍스트 길이를 읽지 않고 자기 설정 파일의 숫자만 믿는다.
그래서 서버 쪽에서 컨텍스트를 바꾸면 클라이언트는 예전 길이까지 대화를 키우고, 서버가 그 요청을
거부한다. 서버 설정을 저장할 때 여기서 같이 고쳐 둔다.

이 파일은 NInfer 설정 UI 와 llama.cpp 설정 UI 에 똑같이 들어 있다 -- 한쪽을 고치면 다른 쪽도 같이.

target (dict) 하나가 서버의 모델 하나다:
  port      서버 포트. 클라이언트 항목의 baseUrl / api 가 localhost:<port> 일 때만 건드린다.
  context   클라이언트가 써도 되는 컨텍스트 길이 (요청 하나 기준)
  vision    이미지 입력 가능 여부
  model_id  서버가 받는 모델 이름. 모르면 None (NInfer 는 model-id 를 안 정하면 이름을 서버만 안다)
  stem      Qwen Code 항목 이름의 괄호 "(<stem>, …)" 로 찾을 때 쓰는 이름 (NInfer artifact). 없으면 None
  label     stem 으로 찾은 항목의 괄호 안 요약 ("180K dflash2"). None 이면 이름은 그대로
  rename    True 면 stem 으로 찾은 Qwen Code 항목의 id 를 model_id 로 바꾼다 (NInfer 에서 model-id 를 정했을 때)

고치는 것은 컨텍스트, 비전, (stem 항목의) 이름 요약과 id 뿐이다. 없는 항목을 새로 만들지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.parse

QWEN_SETTINGS = os.path.join(os.path.expanduser("~"), ".qwen", "settings.json")
HERMES_CONFIG = os.path.join(os.environ.get("HERMES_HOME")
                             or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes"), "config.yaml")
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
BACKUP_SUFFIX = ".bak-client-sync"


def _local_port(url) -> int | None:
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip().strip("'\""))
        return parts.port if parts.hostname in LOCAL_HOSTS else None
    except ValueError:
        return None


def _write(path: str, text: str) -> None:
    """처음 고칠 때 한 번만 백업하고 원자적으로 쓴다. 줄바꿈은 원래 파일의 것을 그대로 둔다(text 에 이미 들어 있다)."""
    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(temp, path)


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except OSError:
        return None


# ───────────────────────── Qwen Code (~/.qwen/settings.json) ─────────────────────────

def plan_qwen(qwen: dict, targets: list[dict]) -> list[dict]:
    """qwen 문서를 고치고 바뀐 항목 목록을 돌려준다. stem 으로 찾은 항목의 id 를 target["model_id"] 에 채운다(Hermes 용)."""
    entries = ((qwen.get("modelProviders") or {}).get("openai")) or []
    changes = []
    for target in targets:
        tag = re.compile(r"\(" + re.escape(target["stem"]) + r"(,[^)]*)?\)") if target.get("stem") else None
        for entry in entries:
            if not isinstance(entry, dict) or _local_port(entry.get("baseUrl")) != int(target["port"]):
                continue
            by_stem = bool(tag and tag.search(str(entry.get("name") or "")))
            if not (by_stem or (target.get("model_id") and entry.get("id") == target["model_id"])):
                continue
            before = json.dumps(entry, sort_keys=True)
            old_id = entry.get("id")
            gen = entry.setdefault("generationConfig", {})
            gen["contextWindowSize"] = int(target["context"])
            if target.get("vision") or "modalities" in gen:
                gen.setdefault("modalities", {})["image"] = bool(target.get("vision"))
            if by_stem and target.get("label"):
                label = f"({target['stem']}, {target['label']})"
                entry["name"] = tag.sub(lambda _: label, entry["name"], count=1)
            if by_stem and target.get("rename") and target.get("model_id") and old_id != target["model_id"]:
                entry["id"] = target["model_id"]
                model = qwen.get("model") or {}
                if model.get("name") == old_id:
                    model["name"] = target["model_id"]
            if by_stem and not target.get("model_id"):
                target["model_id"] = entry.get("id")
            if json.dumps(entry, sort_keys=True) != before:
                changes.append({"client": "qwen", "id": entry.get("id"), "context": int(target["context"]),
                                "vision": bool(target.get("vision"))})
    return changes


def sync_qwen(targets: list[dict], path: str = QWEN_SETTINGS) -> list[dict]:
    text = _read(path)
    try:
        qwen = json.loads(text) if text is not None else None
    except ValueError:
        return []
    if not isinstance(qwen, dict):
        return []
    changes = plan_qwen(qwen, targets)
    if changes:
        nl = "\r\n" if "\r\n" in text else "\n"
        _write(path, json.dumps(qwen, ensure_ascii=False, indent=2).replace("\n", nl) + nl)
    return changes


# ───────────────────────── Hermes Agent (config.yaml) ─────────────────────────
# 주석이 많은 사용자 파일이라 YAML 을 다시 쓰지 않고 해당 줄만 고친다. 블록 매핑(키: 값)만 따라간다.

_KEY = re.compile(r"^(?P<indent> *)(?P<key>\"[^\"]*\"|'[^']*'|[^\s#'\"][^:#]*?):(?P<rest>(?:\s.*)?)$")


def _nodes(lines: list[str]) -> list[dict]:
    """줄마다 {line, indent, key, value, parent} 를 만든다. 키 줄이 아니면(주석, 빈 줄, 목록) 건너뛴다."""
    nodes, stack = [], []
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith(("#", "-")):
            continue
        match = _KEY.match(line)
        if not match:
            continue
        indent = len(match["indent"])
        while stack and stack[-1]["indent"] >= indent:
            stack.pop()
        value = re.sub(r"\s+#.*$", "", match["rest"]).strip()
        node = {"line": index, "indent": indent, "key": match["key"].strip("'\""), "value": value,
                "parent": stack[-1] if stack else None, "children": []}
        if node["parent"]:
            node["parent"]["children"].append(node)
        nodes.append(node)
        stack.append(node)
    return nodes


def _child(node: dict, key: str) -> dict | None:
    return next((c for c in node["children"] if c["key"] == key), None)


def plan_hermes(text: str, targets: list[dict]) -> tuple[str, list[dict]]:
    lines = text.split("\n")
    nodes = _nodes(lines)
    edits: dict[int, str] = {}      # 줄 번호 -> 새 줄
    inserts: dict[int, list[str]] = {}  # 이 줄 뒤에 넣을 줄들
    changes = []

    def set_value(owner: dict, key: str, value: str, create: bool) -> bool:
        node = _child(owner, key)
        if node:
            if node["value"] == value:
                return False
            raw = lines[node["line"]]
            cr = "\r" if raw.endswith("\r") else ""
            comment = re.search(r"\s+#.*$", raw.rstrip("\r"))
            edits[node["line"]] = " " * node["indent"] + f"{key}: {value}" + (comment.group(0) if comment else "") + cr
            return True
        if not create:
            return False
        step = owner["children"][0]["indent"] if owner["children"] else owner["indent"] + 2
        cr = "\r" if lines[owner["line"]].endswith("\r") else ""
        last = owner
        while last["children"]:
            last = last["children"][-1]
        inserts.setdefault(last["line"], []).append(" " * step + f"{key}: {value}" + cr)
        return True

    # 서버를 가리키는 블록: providers.<이름>(api / base_url) 과 최상위 model(base_url)
    blocks = []
    for node in nodes:
        parent = node["parent"]
        if parent is None and node["key"] == "model":
            blocks.append(node)
        elif parent is not None and parent["parent"] is None and parent["key"] in ("providers", "custom_providers"):
            blocks.append(node)
    for target in targets:
        mid = target.get("model_id")
        if not mid:
            continue
        context, vision = str(int(target["context"])), "true" if target.get("vision") else "false"
        for block in blocks:
            url = next((c["value"] for c in block["children"] if c["key"] in ("api", "base_url", "url")), None)
            if _local_port(url) != int(target["port"]):
                continue
            changed = False
            models = _child(block, "models")
            model = models and next((m for m in models["children"] if m["key"] == mid), None)
            if model:
                changed |= set_value(model, "context_length", context, create=True)
                changed |= set_value(model, "supports_vision", vision, create=bool(target.get("vision")))
            default = next((c["value"].strip("'\"") for c in block["children"] if c["key"] in ("default_model", "default")), None)
            if default == mid:
                changed |= set_value(block, "context_length", context, create=False)
            if changed:
                changes.append({"client": "hermes", "id": mid, "context": int(target["context"]), "vision": bool(target.get("vision"))})
    if not edits and not inserts:
        return text, []
    out = []
    for index, line in enumerate(lines):
        out.append(edits.get(index, line))
        out.extend(inserts.get(index, []))
    return "\n".join(out), changes


def sync_hermes(targets: list[dict], path: str = HERMES_CONFIG) -> list[dict]:
    text = _read(path)
    if text is None:
        return []
    new_text, changes = plan_hermes(text, targets)
    if changes and new_text != text:
        _write(path, new_text)
    return changes


def sync_all(targets: list[dict], qwen_path: str = QWEN_SETTINGS, hermes_path: str = HERMES_CONFIG) -> list[dict]:
    """Qwen Code 먼저 (stem 으로 찾은 id 를 Hermes 가 쓴다). 한쪽이 실패해도 다른 쪽은 한다."""
    changes = []
    for sync, path in ((sync_qwen, qwen_path), (sync_hermes, hermes_path)):
        try:
            changes += sync(targets, path)
        except Exception as exc:  # noqa: BLE001 -- 클라이언트 설정 때문에 서버 설정 저장이 실패하면 안 된다
            changes.append({"client": sync.__name__[5:], "error": repr(exc)})
    return changes

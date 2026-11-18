"""Read local Claude Code transcripts without installing hooks or an SDK."""

import json
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import psutil

SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
RECENT_SECONDS = 6 * 3600


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _lines(block):
    for line in block.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict):
            yield event


def _text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return next((part["text"].strip() for part in content
                     if isinstance(part, dict) and part.get("type") == "text"
                     and isinstance(part.get("text"), str) and part["text"].strip()), "")
    return ""


@lru_cache(maxsize=128)
def _transcript(path, size, mtime_ns):
    # shortcut: scan a bounded head and tail, use incremental indexing if large transcripts omit useful titles here.
    with path.open("rb") as stream:
        head = stream.read(256 * 1024)
        if size > len(head):
            offset = max(len(head), size - 512 * 1024)
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                stream.readline()
            head = head[:head.rfind(b"\n") + 1] + stream.read(512 * 1024)

    prompt = title = summary = cwd = step = ""
    status = "unknown"
    for event in _lines(head):
        if event.get("isSidechain") or event.get("isMeta"):
            continue
        kind = event.get("type")
        if isinstance(event.get("cwd"), str) and event["cwd"]:
            cwd = event["cwd"]
        if kind == "custom-title" and isinstance(event.get("customTitle"), str):
            title = event["customTitle"]
        elif kind == "summary" and isinstance(event.get("summary"), str):
            summary = event["summary"]
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        text = _text(content)
        parts = [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []
        if kind == "user":
            if any(part.get("type") == "tool_result" for part in parts):
                status = "executing"
            elif text.startswith("[Request interrupted by user"):
                status, step = "interrupted", ""
            elif text and not text.startswith("<"):
                prompt = prompt or text
                status, step = "executing", ""
        elif kind == "assistant":
            tool = next((part.get("name") for part in reversed(parts) if part.get("type") == "tool_use"), None)
            if event.get("isApiErrorMessage"):
                status, step = "error", text
            elif tool in ("AskUserQuestion", "ExitPlanMode"):
                status, step = "interactive", "等待确认"
            elif isinstance(tool, str):
                status, step = "executing", "执行: " + tool
            elif message.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens", "refusal"):
                status, step = "idle", text or step
            else:
                status, step = "writing", text or step
    name = (title or summary or prompt).strip()
    return name.splitlines()[0][:40] if name else "", cwd, status, step.split("\n", 1)[0][:90]


def _live_sessions(root):
    live = set()
    for path in (root / "sessions").glob("*.json"):
        try:
            item = json.loads(path.read_bytes())
            pid, started = item["pid"], item["startedAt"]
            if type(pid) is int and pid > 0 and psutil.Process(pid).create_time() <= started / 1000 + 2:
                live.add(item["sessionId"])
        except (OSError, ValueError, TypeError, KeyError, psutil.Error):
            continue
    return live


def sessions(seen):
    root = claude_home()
    live = _live_sessions(root)
    now = time.time()
    items = {}
    for path in (root / "projects").glob("*/*.jsonl"):
        sid = path.stem
        if not SESSION_ID.fullmatch(sid):
            continue
        try:
            stat = path.stat()
            stamp = stat.st_mtime_ns // 1_000_000
            if sid not in live and now - stat.st_mtime >= RECENT_SECONDS:
                continue
            if sid in items and items[sid]["stamp"] >= stamp:
                continue
            name, cwd, status, step = _transcript(path, stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
        if not name:
            continue
        active = status in ("executing", "writing", "interactive")
        if active and sid not in live:
            status, active = "unknown", False
        items[sid] = {"id": sid, "name": name, "path": cwd, "status": status, "step": step,
                      "source": "claude", "stamp": stamp,
                      "unread": not active and stamp > seen.get("claude:" + sid, 0)}
    return list(items.values())


def resume_session(session_id, cwd):
    if not SESSION_ID.fullmatch(session_id) or not cwd or not Path(cwd).is_dir():
        return False
    native = Path.home() / ".local" / "bin" / "claude.exe"
    command = shutil.which("claude.exe") or (str(native) if native.is_file() else shutil.which("claude.cmd"))
    if not command:
        return False
    subprocess.Popen([command, "--resume", session_id], cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True

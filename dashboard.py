import ctypes
import ctypes.wintypes
import csv
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import winreg
import sqlite3
from datetime import date, datetime, timezone
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import psutil
import pythoncom
import requests
import win32api
import yaml
import win32com.client
import claude_code
from panel_paths import DATA_DIR, ROOT, STOP_FILE, prepare_data
from panel_watchdog import startup_tasks

# 副屏「每次重绘都闪两下」的处置：QWebEngine 默认走 GPU 合成，每 2 秒一次的数据更新
# 都要提交新的合成帧、动 D3D 交换链，ScreenXpert 的转发层跟不上就黑一下再补一帧，
# 肉眼就是成对闪两下（已验证：把面板藏起来就不闪了，说明触发源是重绘本身）。
# 改成软件光栅化 + 软件合成后，更新变成整窗位图 blit，不再重建交换链。
# 代价是 CPU 略高，但 2880x864 的深色静态看板完全吃得住。设 PANEL_GPU=1 可恢复默认。
# 必须在 import QtWebEngineWidgets 之前设置 —— Chromium 只在首次初始化时读这些开关。
if os.environ.get("PANEL_GPU") != "1":
    _flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--disable-gpu" not in _flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            _flags + " --disable-gpu --disable-gpu-compositing --disable-gpu-rasterization").strip()

from PyQt5 import QtCore, QtGui, QtWidgets, QtWebChannel, QtWebEngineWidgets

STARTUP = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/副屏看板.lnk"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
STATE_FILE = DATA_DIR / "traffic-state.json"
CLASH_CONFIG = Path(os.environ["APPDATA"]) / "io.github.clash-verge-rev.clash-verge-rev" / "clash-verge.yaml"
TUN_STATE = DATA_DIR / "tun-state.json"
DISPLAY_STATE = DATA_DIR / "display.json"
DEEPSEEK_CONFIG = DATA_DIR / "deepseek.json"
# 预览亮度持久化：亮度滑块只在拖动时改 CSS，不落盘；每次启动都回到 HTML 默认 68，
# 故单独存到 brightness.json，启动/刷新时由前端 get_brightness 取回恢复。
BRIGHTNESS_CONFIG = DATA_DIR / "brightness.json"
READ_BEHAVIOR_CONFIG = DATA_DIR / "read-behavior.json"
BUDGET_FILE = DATA_DIR / "quota-day.json"
# 会话回执及看板确认记录，按会话更新时间区分轮次。
SEEN_FILE = DATA_DIR / "seen-sessions.json"
SYSTEM_DRIVE = Path(os.environ["SystemRoot"]).drive or Path.home().drive
# 面板最多列出的会话条数，防止刷屏
MAX_SESSIONS = 10
_SEEN_LOCK = threading.Lock()


def seen_load():
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return {key: value for key, value in data.items()
                if isinstance(value, int) and value >= 0} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def seen_mark(source, session_id, stamp):
    # 采集与点击线程共享已读记录，合并与原子替换必须在同一把锁内。
    with _SEEN_LOCK:
        data = seen_load()
        key = f"{source}:{session_id}"
        if stamp:
            data[key] = max(data.get(key, 0), int(stamp))
        else:
            data.pop(key, None)
        try:
            temp = SEEN_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, SEEN_FILE)
            return True
        except OSError:
            return False


def clash_counters():
    try:
        config = yaml.safe_load(CLASH_CONFIG.read_text(encoding="utf-8"))
        controller = config["external-controller"]
        host = controller.rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            return None
        response = requests.get("http://" + controller + "/connections",
            headers={"Authorization": "Bearer " + str(config.get("secret", ""))},
            timeout=2, proxies={"http": "", "https": ""})
        response.raise_for_status()
        data = response.json()
        return [int(data["downloadTotal"]), int(data["uploadTotal"])]
    except (OSError, KeyError, ValueError, TypeError, AttributeError, requests.RequestException, yaml.YAMLError):
        return None


@lru_cache(maxsize=128)
def tool_label(name, payload_input):
    name = (name or "").lower()
    labels = {
        "exec": "执行命令", "shell": "执行命令", "apply_patch": "编辑文件",
        "file_search": "搜索文件", "web_search": "联网搜索", "web_fetch": "抓取网页",
        "read_file": "读取文件", "write_file": "写入文件",
    }
    label = labels.get(name, name or "处理中")
    if name in ("exec", "shell") and isinstance(payload_input, str):
        match = re.search(r"cmd:\s*[`\"'](.*?)[`\"']", payload_input, re.S)
        if match:
            command = match.group(1).strip().splitlines()
            if command:
                return "执行: " + command[0][:60]
    return label


# 每个 rollout 文件的增量读取状态：pos=已消费到的字节偏移，step/activity/terminal 是
# 按 event 流累计出的当前值。每 2 秒一轮的采集若每次重扫 512KB 尾部、每行 JSON 解析
# 两遍，多会话叠加能把单核吃满，所以只解析新增行，一次遍历同时更新 step 与 activity。
_ROLLOUT_STATE = {}
_ROLLOUT_STATE_CAP = 512

_ITEM_ACTIVITY = {
    "websearch": "researching", "filechange": "writing", "agentmessage": "writing",
    "mcptoolcall": "syncing", "commandexecution": "executing",
}

QUESTION_TOOLS = ("request_user_input", "request_user_input_async", "AskUserQuestion",
                  "ask_user_question", "question")


def _rollout_status(state):
    # 与旧的“尾部倒序扫描”语义一致：最近一次 task_started/complete/aborted 决定状态，
    # running 时用最近一次条目活动细化（尚无条目则 executing）。
    if state.get("question_call") and state["terminal"] not in ("error", "interrupted"):
        return "interactive"
    if state["terminal"] == "running":
        return state["activity"] or "executing"
    if state["terminal"] in ("idle", "error", "interrupted"):
        return state["terminal"]
    return state["activity"]


def rollout_progress(path, size, modified):
    key = str(path)
    state = _ROLLOUT_STATE.get(key)
    if state is not None and (size < state["pos"] or
                             (size == state["pos"] and modified != state["modified"])):
        state = None  # 日志被轮转/截断：状态作废，从尾部重建
    if state is None:
        state = {"pos": max(0, size - 512 * 1024), "step": None, "turn_id": None,
                 "terminal": None, "activity": None, "used": time.time()}
        _ROLLOUT_STATE[key] = state
        if len(_ROLLOUT_STATE) > _ROLLOUT_STATE_CAP:
            oldest = min(_ROLLOUT_STATE, key=lambda k: _ROLLOUT_STATE[k]["used"])
            _ROLLOUT_STATE.pop(oldest, None)
    state["used"] = time.time()
    state["modified"] = modified
    try:
        with Path(path).open("rb") as stream:
            stream.seek(state["pos"])
            data = stream.read(max(0, size - state["pos"]))
    except OSError:
        return state["turn_id"], _rollout_status(state), state["step"]
    cut = data.rfind(b"\n")          # 只消费完整行，半行留到下次
    if cut < 0:
        return state["turn_id"], _rollout_status(state), state["step"]
    state["pos"] += cut + 1
    for line in data[:cut].splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        record_type = record.get("type")
        if record_type == "event_msg":
            if kind == "task_complete":
                state.update(terminal="idle", turn_id=payload.get("turn_id"))
            elif kind == "turn_aborted":
                state.update(terminal={"failed": "error", "interrupted": "interrupted"}.get(payload.get("reason"), "idle"),
                             turn_id=payload.get("turn_id"), question_call=None)
            elif kind == "task_started":
                state.update(terminal="running", turn_id=payload.get("turn_id"), activity=None,
                             step=None, question_call=None)
            elif kind == "user_message":
                state.update(question_call=None, activity="executing", step=None)
            elif kind in ("item_started", "item_completed"):
                item = payload.get("item") or {}
                if not isinstance(item, dict):
                    continue
                mapped = _ITEM_ACTIVITY.get((item.get("type") or "").replace("_", "").lower())
                if mapped:
                    state["activity"] = mapped
        elif record_type == "response_item":
            if kind == "message" and payload.get("role") == "user":
                state.update(question_call=None, activity="executing", step=None)
            elif kind == "function_call_output" and payload.get("call_id") == state.get("question_call"):
                if not state.get("question_async"):
                    state.update(question_call=None, activity="executing", step=None)
            elif kind == "message" and payload.get("role") == "assistant":
                # 步骤预览取最新一条助手消息首行（末次命中即最新）
                for content in payload.get("content", []):
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") == "output_text" and content.get("text"):
                        if not state.get("question_call"):
                            state["step"] = content["text"].strip().split("\n", 1)[0].strip()[:90]
                        break
                state["activity"] = "writing"
            elif kind == "web_search_call":
                state["activity"] = "researching"
            elif kind in ("function_call", "custom_tool_call"):
                name = payload.get("name") or ""
                state["activity"] = ("researching" if "web_run" in name
                                     else "writing" if name == "apply_patch" else "executing")
                if kind == "custom_tool_call" and not state.get("question_call"):
                    state["step"] = tool_label(payload.get("name"), payload.get("input"))
                if name.rsplit(".", 1)[-1].rsplit("__", 1)[-1] in QUESTION_TOOLS:
                    state.update(question_call=payload.get("call_id"),
                                 question_async=name.endswith("_async"), step="等待你的回答或确认")
                    try:
                        args = json.loads(payload.get("arguments") or "{}")
                        question = args["questions"][0]
                        state["step"] = str(question.get("question") or question.get("title")
                                            or state["step"])[:90]
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        pass
    return state["turn_id"], _rollout_status(state), state["step"]


def unread_sessions():
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    seen = seen_load()
    with closing(sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=2)) as state, \
         closing(sqlite3.connect((root / "thread_history_1.sqlite").as_uri() + "?mode=ro", uri=True, timeout=2)) as history:
        receipt_columns = {column[1] for column in state.execute("PRAGMA table_info(thread_read_receipts)")}
        revision_col = "r.revision" if "revision" in receipt_columns else "NULL"
        rows = state.execute(f"""SELECT t.id, COALESCE(NULLIF(t.name,''), t.title), t.rollout_path, t.updated_at,
                   r.first_unread_turn, r.last_published_turn, {revision_col}
            FROM threads t LEFT JOIN thread_read_receipts r ON r.thread_id=t.id
            WHERE t.archived=0 AND COALESCE(t.thread_source,'') <> 'subagent'
            ORDER BY t.updated_at DESC""").fetchall()
        result = []
        for thread_id, title, path, updated, unread, published, revision in rows:
            # 当前回合（DB 官方状态）：取该 thread 最新一条 turn 的 status
            db_status = None
            db_row = None
            try:
                db_row = history.execute(
                    "SELECT status, turn_id FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
                    (thread_id,)).fetchone()
                db_status = db_row[0] if db_row else None
            except sqlite3.Error:
                db_status = None
            mtime = 0.0
            activity, step, turn_id = None, None, None
            try:
                stat = Path(path).stat()
                mtime = stat.st_mtime
                # 官方回合仍在跑（inProgress）时无视 4h 静默窗口，强制读日志取实时活动/步骤，
                # 否则长任务静默超过 4h 会因 activity 回落成 None 而被判成终态、条目直接消失。
                if unread or (time.time() - mtime) < 4 * 3600 or db_status == "inProgress":
                    turn_id, activity, step = rollout_progress(path, stat.st_size, stat.st_mtime_ns)
                    if db_row and turn_id and turn_id != db_row[1]:
                        known = history.execute(
                            "SELECT 1 FROM thread_turns WHERE thread_id=? AND turn_id=?",
                            (thread_id, turn_id)).fetchone()
                        if not known:
                            db_status = None  # 日志中的新回合尚未落库，不能套用上一轮终态。
            except OSError:
                pass
            fresh = bool(mtime) and (time.time() - mtime) < 600
            if db_status == "interrupted" and activity == "idle" and db_row and turn_id == db_row[1]:
                db_status = "completed"
            live = activity in ("writing", "researching", "executing", "syncing")
            if db_status == "completed":
                live = False  # 官方回合已结束，别被日志尾部误判成“处理中”
            elif db_status == "inProgress":
                # 不能直接 blind 判 live：Codex 偶发把已结束的回合永久卡在 inProgress，
                # 这类“僵尸”回合通常连回滚日志文件都不存在。仅当回滚日志真实存在、
                # 且解析出仍在跑（activity 属处理中）或一天内仍动过时才算执行中；
                # 否则视为已结束，避免“明明早结束了却还显示执行”。
                if bool(mtime) and activity in ("writing", "researching", "executing", "syncing"):
                    live = True
                elif bool(mtime) and activity is None and (time.time() - mtime) < 24 * 3600:
                    live = True  # 日志存在且一天内动过但当前窗口没解析出具体活动：兜底视为执行中（超大静默 exec）
                    activity = "executing"
                else:
                    live = False  # 日志缺失或远超一天没动：inProgress 实为僵尸，已结束
            # interrupted 只证明中断，不伪装成“待回复”，也不按十分钟猜测已完成。
            if db_status == "interrupted":
                if int(mtime * 1000) <= seen.get("codex:" + thread_id, 0):
                    continue
                status, unread = "interrupted", True
            elif activity == "interactive" and db_status not in ("failed", "interrupted"):
                status, unread = "interactive", True
            elif live:
                status = activity or "executing"   # 没有实时活动细节时也至少显示“执行中”
            elif db_status == "failed" and (unread or fresh):
                status = "error"
            elif unread or db_status == "completed" or (db_status is None and (published or activity == "idle")):
                status = "idle"
            else:
                status = None
            if status is None:
                continue
            stamp = int((mtime or (updated if status == "idle" else 0) or 0) * 1000)
            result.append({"id": thread_id, "name": title, "status": status,
                           "unread": bool(unread), "step": step, "source": "codex",
                           "stamp": stamp, "receipt": revision or 0})
        return result


ACTIVE = ("writing", "researching", "executing", "syncing")
# 「待回复」不算处理中，但同样在等用户操作，必须常驻、也不能被列表上限裁掉。
PINNED = ACTIVE + ("interactive",)


def _session_visible(item):
    if item["status"] in PINNED:
        return True
    return bool(item.get("unread"))


def review_idle_sessions(items, previous, keep_read=True):
    seen = seen_load()
    # 只保留已在看板提醒过的轮次，避免把客户端历史已读会话带进列表。
    previous_by_session = {(item["source"], item["id"]): item for item in previous}
    visible = []
    for item in items:
        source, sid, stamp = item["source"], item["id"], item["stamp"]
        key = f"{source}:{sid}"
        last = previous_by_session.get((source, sid))
        if item["status"] in PINNED:
            # 有些客户端在新轮次开始时仍沿用旧的更新时间。
            for state in ("reviewed", "dismissed"):
                if f"{state}:{key}" in seen:
                    seen_mark(f"{state}:{source}", sid, 0)
        elif item["status"] == "idle" and stamp > 0:
            receipt = item.get("receipt", 0) if source == "codex" else seen.get(key, 0)
            if last and last["status"] in PINNED:
                visible.append(item if item["unread"] else {**item, "unread": True, "read_after": receipt})
                continue
            if stamp <= seen.get("dismissed:" + key, 0):
                continue
            read = not item["unread"] if source == "codex" else stamp <= receipt
            reviewed = stamp == seen.get("reviewed:" + key)
            if last and last["status"] == "idle" and last["stamp"] == stamp and not reviewed:
                read_after = last.get("read_after")
                if read and (read_after is None or receipt > read_after):
                    seen_mark("reviewed:" + source, sid, stamp)
                    reviewed = True
                elif read_after is not None and not item["unread"]:
                    visible.append({**item, "unread": True, "read_after": read_after})
                    continue
            if reviewed:
                if keep_read:
                    visible.append({**item, "read": True, "unread": False})
                continue
        if _session_visible(item):
            visible.append(item)
    return visible


def claude_sessions():
    if not (claude_code.claude_home() / "projects").is_dir():
        raise FileNotFoundError("Claude Code transcripts are unavailable")
    return claude_code.sessions(seen_load())


def cap_sessions(sessions, limit=MAX_SESSIONS):
    """排序 + 上限裁剪：处理中/待回复及待移除的已读条目全部保留。

    旧版一把 [:MAX_SESSIONS] 会把活跃会话挤掉（会话一多就「少显示几条」）。
    """
    sessions = sorted(sessions, key=lambda s: (s["status"] not in PINNED,
                                               -int(s.get("stamp") or 0)))
    pinned = [s for s in sessions if s["status"] in PINNED]
    unread = [s for s in sessions if s["status"] not in PINNED and not s.get("read")]
    read = [s for s in sessions if s.get("read")]
    return pinned + unread[:max(0, limit - len(pinned))] + read


def _is_codex_exe(exe):
    # 桌面版 Codex 实际进程是 WindowsApps 下的 ChatGPT.exe（路径含 openai.codex），
    # 另外两种是 CLI/app-server 的 openai/codex 与旧皮肤版 codexdreamskin。
    return "openai/codex" in exe or "openai.codex" in exe or "codexdreamskin" in exe


def _is_workbuddy_exe(exe):
    return "workbuddy.exe" in exe


def _is_zcode_exe(exe):
    return "/zcode.exe" in exe or exe.endswith("zcode.exe")


def _is_scnet_exe(exe):
    return "/计算服务.exe" in exe or exe.endswith("计算服务.exe")


def _scnet_exe_path():
    # 定位 SCNet 主程序（超算互联网客户端），用于注册 / 启动深链协议。
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        cand = Path(base) / "scnet-client" / "计算服务.exe"
        if cand.exists():
            return str(cand)
    return None


MASK_TITLE = "DisplayChangeMask"
MASK_CLASS = "Static"
_asus_pid_cache = [0.0, frozenset()]


def _asus_pids():
    """AsusLibra.exe 的 pid 集合（30 秒缓存）。

    遮罩窗口只可能由这个进程创建，先按 pid 把候选收窄到个位数，
    后面的读标题才有资格做 —— 全量窗口逐个读标题是上一版的致命错误。
    """
    if time.monotonic() - _asus_pid_cache[0] < 30 and _asus_pid_cache[1]:
        return _asus_pid_cache[1]
    pids = set()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() == "asuslibra.exe":
                pids.add(proc.pid)
        except psutil.Error:
            continue
    _asus_pid_cache[0] = time.monotonic()
    _asus_pid_cache[1] = frozenset(pids)
    return _asus_pid_cache[1]


def _window_title(hwnd, timeout_ms=20):
    """读窗口标题，但绝不阻塞调用方。

    GetWindowTextW 对其它进程的窗口是发 WM_GETTEXT 同步等回复；ScreenXpert 更新后
    会出现长时间不处理消息的窗口，这一等就把 Qt 主线程挂死 —— 表现是面板不重绘、
    心跳断流、整个副屏黑掉而且"卡"。改用 SendMessageTimeoutW，超时即放弃。
    """
    user32 = ctypes.windll.user32
    send = user32.SendMessageTimeoutW
    send.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM,
                     ctypes.wintypes.LPARAM, ctypes.wintypes.UINT, ctypes.wintypes.UINT,
                     ctypes.POINTER(ctypes.c_size_t)]
    send.restype = ctypes.c_ssize_t
    buf = ctypes.create_unicode_buffer(64)
    WM_GETTEXT, SMTO_ABORTIFHUNG = 0x000D, 0x0002
    out = ctypes.c_size_t()
    try:
        ok = send(hwnd, WM_GETTEXT, 64, ctypes.addressof(buf),
                  SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(out))
    except Exception:
        return ""
    return buf.value if ok else ""


def _find_display_mask():
    """找到 ScreenXpert 的显示切换遮罩窗口，没有则返回 0。

    只枚举 AsusLibra 进程、class 为 Static 的窗口再比对标题，全程不阻塞。
    """
    user32 = ctypes.windll.user32
    pids = _asus_pids()
    if not pids:
        return 0
    hit = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def enum_cb(hwnd, _):
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != MASK_CLASS:
            return True
        if _window_title(hwnd) == MASK_TITLE:
            hit.append(hwnd)
            return False
        return True

    try:
        user32.EnumWindows(proc(enum_cb), 0)
    except Exception:
        return 0
    return hit[0] if hit else 0


def demote_display_mask(state):
    """把 ScreenXpert 的显示切换遮罩持续压到置顶层之下。

    ScreenPadMaster 4.0.36（AsusLibra.exe）会周期性把一个铺满副屏的 topmost
    layered 窗口（标题 DisplayChangeMask，2880x864，像素全透明）顶到所有窗口之上、
    几秒后再收回，实测约每 10~15 秒一轮。它压在看板之上，每顶一次整块副屏就要
    重新合成一次 —— 表现就是「整个页面在闪」，合成器负载飙升时则是「很卡」。

    看板本身就是被盖住的那个窗口，所以由它把遮罩清掉 WS_EX_TOPMOST 并推到最底层，
    遮罩重新出现时也只会落在看板下面，不再造成整屏重合成。

    state 是可变的单元素列表，缓存已找到的 hwnd，避免每次都全量枚举窗口。
    返回本次是否真的执行了降级。
    """
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    HWND_BOTTOM = 1
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

    hwnd = state[0]
    if not hwnd or not user32.IsWindow(hwnd):
        hwnd = _find_display_mask()
        state[0] = hwnd
        if not hwnd:
            return False
    try:
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not ex & WS_EX_TOPMOST:
            return False
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_TOPMOST)
        user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        return True
    except Exception:
        state[0] = 0
        return False


_K32 = ctypes.windll.kernel32
_K32.OpenProcess.restype = ctypes.wintypes.HANDLE
_K32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                             ctypes.wintypes.DWORD]
_K32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
_K32.QueryFullProcessImageNameW.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
                                            ctypes.wintypes.LPWSTR,
                                            ctypes.POINTER(ctypes.wintypes.DWORD)]
_K32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# 上次成功置前的窗口：matches.__name__ -> (hwnd, pid)。命中时整次点击只剩一次
# IsWindow + 一次进程路径校验，连 EnumWindows 都不必跑。
_focus_cache = {}


def _process_exe_fast(pid):
    """取进程完整路径（小写、正斜杠），只用 Win32，不构造 psutil.Process。

    实测（857 个顶层窗口 / 500 个进程）：
      psutil.Process(pid).exe() 逐窗口调用 = 4530 ms，遇到无响应进程还会偶发挂住；
      psutil.process_iter 全量遍历 = 2290~3210 ms，同样不能放在点击路径上；
      先按窗口特征粗筛后，QueryFullProcessImageNameW 解析 33 个 pid = 0.8 ms。
    """
    if not pid:
        return ""
    handle = _K32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(1024)
        if not _K32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.replace("\\", "/").lower()
    finally:
        _K32.CloseHandle(handle)


class _WindowPlacement(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint), ("ptMinPosition", ctypes.wintypes.POINT),
                ("ptMaxPosition", ctypes.wintypes.POINT),
                ("rcNormalPosition", ctypes.wintypes.RECT)]


def _window_area(hwnd):
    """窗口面积按「还原后」尺寸算。

    最小化的 Electron 窗口 GetWindowRect 会返回 (-16000,-16000) 附近约 160x25 的假矩形，
    旧逻辑用 area >= 400*300 收窄候选时把它整个丢掉 → 误判客户端没开 → 去启动新实例。
    GetWindowPlacement.rcNormalPosition 才是最小化前的真实矩形。
    """
    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0
    if user32.IsIconic(hwnd):
        placement = _WindowPlacement()
        placement.length = ctypes.sizeof(placement)
        get_placement = user32.GetWindowPlacement
        get_placement.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(_WindowPlacement)]
        get_placement.restype = ctypes.c_bool
        if get_placement(hwnd, ctypes.byref(placement)):
            rect = placement.rcNormalPosition
    return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)


def _client_running(fragment):
    """客户端进程是否还活着（只按进程名判断，供「窗口找不到」时兜底）。"""
    try:
        return any(fragment in (proc.info.get("name") or "").lower()
                   for proc in psutil.process_iter(["name"]))
    except (psutil.Error, OSError):
        return False


def _activate_window(hwnd):
    """把选中的窗口提到前台（从托盘隐藏态拉回 / 最小化态复原）。"""
    user32 = ctypes.windll.user32
    if not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 5)           # SW_SHOW：保留最大化状态
    elif user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)           # SW_RESTORE：已最大化的别动
    try:
        user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY：允许后台进程置前
        user32.SetForegroundWindow(hwnd)
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _focus_window(matches):
    # 把匹配的顶层窗口提到前台，返回是否找到。
    # 客户端“关到托盘”后主窗口是隐藏（不可见但仍在）而不是最小化，所以隐藏的主窗口
    # 也要算候选 —— 否则会退回协议启动，又开一个新窗口。
    # 关键：枚举回调里绝不做跨进程调用。旧版对 857 个窗口逐个调
    # psutil.Process(pid).exe()，耗时 4.5 秒且跑在 Qt 主线程上，正是
    # 「点击后半天没反应 / 整个面板卡死」的根因。
    user32 = ctypes.windll.user32
    key = getattr(matches, "__name__", str(matches))
    cached = _focus_cache.get(key)
    if cached:
        hwnd, pid = cached
        cur = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(cur))
        if (user32.IsWindow(hwnd) and cur.value == pid
                and matches(_process_exe_fast(pid))):
            return _activate_window(hwnd)
        _focus_cache.pop(key, None)

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    rough = []

    def enum_cb(hwnd, _):
        # 先用纯本地 Win32 调用把 857 个窗口收窄到几十个「像主窗口」的，再只对
        # 它们的 pid 解析进程路径。GetWindowTextLengthW 对外部窗口读缓存标题、
        # 不发 WM_GETTEXT，安全；GetWindowTextW 会发同步消息，绝对别用。
        visible = bool(user32.IsWindowVisible(hwnd))
        titled = user32.GetWindowTextLengthW(hwnd) > 0
        area = _window_area(hwnd)
        # 只按面积收窄，不再要求「可见或有标题」：客户端关到托盘后主窗口常常是
        # 隐藏且无标题（ZCode 实测如此），旧版因此找不到窗口、反而去启动新实例。
        # 这类窗口在排序里 visible/titled 都记 1，只在没有正常窗口时兜底入选。
        if area < 400 * 300:
            return True
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rough.append((hwnd, pid.value, visible, titled, area))
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
    except Exception:
        return False

    exes = {}
    candidates = []
    for hwnd, pid, visible, titled, area in rough:
        if pid not in exes:                 # 同 pid 只解析一次（多窗口应用有几十个 pid）
            exes[pid] = _process_exe_fast(pid)
        if not matches(exes[pid]):
            continue
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        # Codex 的 computer-use overlay 也是大窗口，按面积可能盖过真正的主窗口，
        # 把它弹到前台会让人以为「点了没反应」。
        if "overlay" in cls.value.lower():
            continue
        # 隐藏且无标题的托盘窗口只作兜底：有正常窗口时永远排在后面。
        fallback = 0 if (visible or titled) else 1
        candidates.append((fallback, 0 if visible else 1, 0 if titled else 1,
                           0 if user32.IsZoomed(hwnd) else 1, -area, hwnd, pid))
    if not candidates:
        return False
    candidates.sort(key=lambda item: item[:6])
    hwnd, pid = candidates[0][5], candidates[0][6]
    _focus_cache[key] = (hwnd, pid)
    return _activate_window(hwnd)


def _diag_url(text):
    # 协议投递留痕：再出现「Windows 找不到 '\\' 文件」时，能直接判断是不是面板投的 URL。
    try:
        message = "%s URL %s\n" % (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), text)
        if _diag is not None:
            _diag.write(message)
        else:
            with (DATA_DIR / "dash_diag.log").open("a", encoding="utf-8") as stream:
                stream.write(message)
    except OSError:
        pass


def _protocol_handler(scheme):
    """协议在本机注册的 shell\\open\\command；没有注册返回空串。"""
    for hive, base in ((winreg.HKEY_CURRENT_USER, "Software\\Classes\\" + scheme),
                       (winreg.HKEY_CLASSES_ROOT, scheme)):
        try:
            key = winreg.OpenKey(hive, base + "\\shell\\open\\command")
            value = winreg.QueryValue(key, "") or ""
            winreg.CloseKey(key)
            if value:
                return value
        except OSError:
            continue
    return ""


# 只投递有会话路由的协议；ZCode 尚无可用的会话级入口。
_DEEP_LINK_SCHEMES = {"workbuddy", "codex", "scnet"}
_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _open_url(url):
    # 实测：只有 explorer.exe 能把 URL 投递给已运行的客户端（os.startfile / 直接
    # Popen(exe, url) 在这台机器上会被静默丢掉，日志里连深链事件都没有）。
    scheme = url.split(":", 1)[0].lower()
    if scheme not in _DEEP_LINK_SCHEMES or not _protocol_handler(scheme):
        _diag_url("BLOCK " + url)
        return False
    _diag_url("OPEN " + url)
    try:
        subprocess.Popen(["explorer.exe", url], creationflags=NO_WINDOW)
    except OSError:
        return False
    return True


_WB_CLICK_LOCK = threading.Lock()
_WB_LAST_CLICK = {}


def _open_workbuddy_session(session_id):
    # 限制重复深链；已读只能由实际打开事件或用户显式确认产生。
    now = time.time()
    with _WB_CLICK_LOCK:
        if now - _WB_LAST_CLICK.get(session_id, 0) < 20:
            return
        _WB_LAST_CLICK[session_id] = now
    _focus_window(_is_workbuddy_exe)
    delivered = _open_url("workbuddy://chat/" + session_id)
    if not delivered:
        with _WB_CLICK_LOCK:
            if _WB_LAST_CLICK.get(session_id) == now:
                _WB_LAST_CLICK.pop(session_id, None)
    return delivered


_codex_cdp_port = [9335]  # 上次成功的端口，下次优先试它，省掉全端口扫描


def _codex_debug_target():
    # 仅作协议失败时的备用入口；正常启动的 Codex 可能没有开放这些调试端口。
    # 本地 CDP 正常响应是毫秒级；端口被占却不响应时 0.4s 就放弃。实测 CDP 全关时
    # 旧版是 3 端口 × 1.2s = 3.6s（失败还要重试一轮 = 7.2s），现在最多 1.2s。
    preferred = _codex_cdp_port[0]
    ports = [preferred] + [p for p in (9335, 9333, 9222) if p != preferred]
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.4) as resp:
                targets = json.loads(resp.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        for target in targets:
            if target.get("type") == "page" and "index.html" in (target.get("url") or ""):
                _codex_cdp_port[0] = port
                return target.get("webSocketDebuggerUrl")
    return None


def _codex_click_thread(thread_id, title=""):
    # 协议不可用时，在已开放 CDP 的 Codex 窗口里按精确 ID 或唯一完整标题定位。
    endpoint = _codex_debug_target()
    if not endpoint:
        return False
    try:
        from websocket import create_connection
    except ImportError:
        return False
    script = """(() => {
  const id = %s, title = %s;
  const rows = [...document.querySelectorAll('[data-app-action-sidebar-thread-id]')];
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const byId = rows.find(r => {
    const key = r.getAttribute('data-app-action-sidebar-thread-id') || '';
    return key === id || key.endsWith(':' + id);
  });
  const titled = title ? rows.filter(r => norm(r.getAttribute('title') ||
    r.querySelector('[title]')?.getAttribute('title') || r.innerText) === norm(title)) : [];
  const byTitle = titled.length === 1 ? titled[0] : null;
  const el = byId || byTitle;
  if (!el) return false;
  el.scrollIntoView({block: 'center'});
  el.click();
  return true;
})()""" % (json.dumps(thread_id), json.dumps(title or ""))
    try:
        ws = create_connection(endpoint, timeout=1.2)
    except Exception:
        return False
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": script, "returnByValue": True}}))
        # Codex 忙（回合执行中）时 evaluate 响应会变慢。窗口已聚焦时失败也不会退到
        # 深链（那会开新窗口），最多只是同条目再点一次，所以 1.2s 收手即可，
        # 不必像旧版那样干等 3.5s。
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                ws.settimeout(max(0.2, deadline - time.time()))
                message = json.loads(ws.recv())
            except Exception:
                break
            if message.get("id") == 1:
                return bool(message.get("result", {}).get("result", {}).get("value"))
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass


CODEX_EXE_CACHE = DATA_DIR / "codex-exe-path.json"


def _codex_exe_path():
    # WindowsApps 目录禁止列举（os.listdir 直接 WinError 5），但具体文件可以直接
    # 访问，所以不能 glob。改为从运行中的 ChatGPT.exe 拿真实路径并缓存到项目里：
    # 目录名带版本号，Codex 升级后缓存会失效，但只要它再运行一次就会自动更新。
    try:
        cached = json.loads(CODEX_EXE_CACHE.read_text(encoding="utf-8")).get("exe")
    except (OSError, ValueError, AttributeError):
        cached = None
    if cached and Path(cached).exists():
        return cached
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if (proc.info.get("name") or "").lower() != "chatgpt.exe":
                    continue
                exe = proc.info.get("exe") or ""
                if exe and "OpenAI.Codex_" in exe and Path(exe).exists():
                    try:
                        CODEX_EXE_CACHE.write_text(json.dumps({"exe": exe}), encoding="utf-8")
                    except OSError:
                        pass
                    return exe
            except (psutil.Error, OSError):
                continue
    except (psutil.Error, OSError):
        pass
    return None


def _ensure_codex_protocol():
    """补齐 codex:// 缺失的 shell\\open\\command（幂等，路径变了才重写）。

    本机 HKCR\\codex 只有 URL Protocol 声明、没有 command，任何程序打开
    codex://threads/<id> 都会被 explorer 当成 UNC 路径 "\\\\"，弹出
    「Windows 找不到 '\\\\' 文件」。补上 command 后开机恢复/Edge 恢复链接
    也不会再报错。WindowsApps 路径带版本号，所以每次启动自检一次。
    """
    handler = _protocol_handler("codex")
    if handler and "openai.codex_" not in handler.lower():
        return
    exe = _codex_exe_path()
    if not exe:
        return
    command = '"' + exe + '" "%1"'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Classes\codex\shell\open\command")
        current = winreg.QueryValue(key, "") or ""
        winreg.CloseKey(key)
        if current == command:
            return
    except OSError:
        pass
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Classes\codex\shell\open\command")
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
        winreg.CloseKey(key)
        _diag_url("FIX codex protocol -> " + exe)
    except OSError:
        return


def _codex_open_session(session_id, title):
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return False
    # 正常启动的 Codex 不开放 CDP；会话深链按 ID 定位，也不受侧栏折叠和同名标题影响。
    _ensure_codex_protocol()
    if _open_url("codex://threads/" + session_id):
        return True
    _focus_window(_is_codex_exe)
    if _codex_click_thread(session_id, title):
        return True
    time.sleep(0.3)
    return _codex_click_thread(session_id, title)


def zcode_step(session_id):
    # 从 zcode 的 model-io rollout 取最后一条 assistant 的思考/回复/工具调用作为当前步骤预览
    p = Path(os.environ["USERPROFILE"]) / ".zcode" / "cli" / "rollout" / f"model-io-{session_id}.jsonl"
    if not p.exists():
        return None
    try:
        size = p.stat().st_size
        start = max(0, size - 200 * 1024)
        with p.open("rb") as stream:
            stream.seek(start)
            lines = stream.read(size - start).splitlines()
        if start:
            lines = lines[1:]
        # 反向找最近一条能给出步骤的请求：最后一行常是 delta 流式请求（不带 assistant 消息），需向前回退
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            request = record.get("request") or {}
            messages = request.get("messages")
            if messages is None:
                messages = (request.get("body") or {}).get("messages") or []
            for m in reversed(messages):
                if m.get("role") != "assistant":
                    continue
                # 思考（reasoning）优先于正文回复；content 可能是块列表或纯字符串
                blocks = m.get("content")
                thoughts, texts = [], []
                for block in (blocks if isinstance(blocks, list) else []):
                    if isinstance(block, dict) and block.get("text"):
                        (thoughts if block.get("type") == "reasoning" else texts).append(block["text"])
                pick = thoughts[-1] if thoughts else (texts[-1] if texts else blocks)
                if isinstance(pick, str) and pick.strip():
                    return pick.strip().split("\n", 1)[0].strip()[:90]
                tool_calls = m.get("tool_calls") or m.get("toolCalls")
                if tool_calls:
                    fn = tool_calls[0].get("function") or {}
                    name = fn.get("name") or tool_calls[0].get("name")
                    if name:
                        return ("调用 " + name)[:90]
        return None
    except (OSError, ValueError):
        return None


def zcode_tool_running(conn, session_id, last_done, horizon_ms=24 * 3600 * 1000):
    # 最新一条工具调用仍在跑（status='running' 且未落 completed_at）→ 会话必然在执行。
    # agent 长时间跑命令/训练期间没有模型请求，model-io 会静默远超 zcode_live 的 300 秒
    # 窗口，条目会闪回「待命」；tool_usage 的 running 行是比 model-io 更硬的在跑证据。
    # 只认 24h 内开始、且晚于上一轮结束时刻的：进程被硬杀会遗留 running 行，
    # 不能让这种死会话永远钉在「执行中」。
    try:
        row = conn.execute(
            "SELECT started_at FROM tool_usage WHERE session_id=? AND status='running' "
            "AND completed_at IS NULL AND started_at>? ORDER BY started_at DESC LIMIT 1",
            (session_id, last_done)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0] and time.time() * 1000 - row[0] < horizon_ms)


def zcode_waiting_question(conn, session_id, last_done):
    try:
        placeholders = ",".join("?" for _ in QUESTION_TOOLS)
        row = conn.execute(
            "SELECT tool_name, started_at FROM tool_usage WHERE session_id=? "
            "AND status='running' AND completed_at IS NULL AND started_at>? "
            f"AND tool_name IN ({placeholders}) ORDER BY started_at DESC LIMIT 1",
            (session_id, last_done, *QUESTION_TOOLS)).fetchone()
        return bool(row and time.time() * 1000 - row[1] < 24 * 3600 * 1000)
    except sqlite3.Error:
        return False


def zcode_live(session_id, updated, last_done=0):
    # ZCode 的 turn_usage 只在每轮结束后落库（无 inProgress 记录），单看它会把正在跑的会话
    # 误判成 idle。用两个实时信号兜底：model-io 日志在“上一轮结束之后”又被追加（每次模型
    # 请求都写）、会话在上一轮结束后又更新。带上 last_done 就不会在刚结束的 3 分钟里一直闪。
    try:
        rollout = Path(os.environ["USERPROFILE"]) / ".zcode" / "cli" / "rollout" / f"model-io-{session_id}.jsonl"
        mtime = rollout.stat().st_mtime * 1000
        # 300 秒而不是更短：agent 长时间跑工具（命令/测试）期间没有模型请求，model-io
        # 会停更几分钟，窗口太短会闪回“待命”；回合真结束时 turn_usage 会落库把状态压回去。
        if mtime > last_done + 2000 and time.time() * 1000 - mtime < 300 * 1000:
            return True
    except OSError:
        pass
    return bool(updated) and updated > last_done + 2000 and time.time() - updated / 1000 < 90


def zcode_sessions():
    # ZCode 桌面程序会话（~/.zcode/cli/db/db.sqlite），与 Codex 同源，可显示处理中/步骤。
    db = Path(os.environ["USERPROFILE"]) / ".zcode" / "cli" / "db" / "db.sqlite"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT id, NULLIF(title,''), directory, time_updated, time_archived "
            "FROM session WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT 60").fetchall()
        result = []
        seen_cache = seen_load()
        resumes = zcode_resume_seen()
        # 老版本 schema 没有 completed_at，退回 started_at 做“上一轮结束时刻”的近似
        columns = {row[1] for row in conn.execute("PRAGMA table_info(turn_usage)")}
        done_col = "completed_at" if "completed_at" in columns else "started_at"
        for sid, title, directory, updated, archived in rows:
            # turn_usage 只在轮次结束后落库：最新一条的 completed_at 之后若日志又被写过，
            # 说明新一轮已经在跑，要显示处理中（否则整轮都显示 idle）。
            turn = conn.execute(
                f"SELECT status, {done_col} FROM turn_usage WHERE session_id=? "
                "ORDER BY started_at DESC LIMIT 1", (sid,)).fetchone()
            turn_status = turn[0] if turn else None
            last_done = int(turn[1] or 0) if turn else 0
            # 实时信号优先：上一轮结束后仍有模型请求 / 会话更新 / 工具仍在跑 → 执行中。
            # 历史 error 不能盖住新一轮正在跑的事实（表现：明明在跑却一直显示异常）；
            # 只有“刚落下”的 error 才压过实时信号——error 落库瞬间 session.updated 可能
            # 比 completed_at 晚 2s+，会触发 zcode_live 的 90 秒兜底而误判成执行中。
            error_fresh = turn_status == "error" and time.time() * 1000 - last_done < 90_000
            if zcode_waiting_question(conn, sid, last_done):
                status = "interactive"
            elif (zcode_tool_running(conn, sid, last_done)
                    or (not error_fresh and zcode_live(sid, updated, last_done))):
                status = "executing"
            elif turn_status == "error":
                status = "error"
            else:
                status = "idle"
            # 无标题时退回目录尾段，避免显示整个项目路径
            name = title or (directory or "").replace("\\", "/").rsplit("/", 1)[-1] or "ZCode 会话"
            # ZCode 用恢复会话的日志事件或显式确认判断已读，终态提醒保留六小时。
            key = "zcode:" + sid
            seen = seen_cache.get(key, 0)
            resumed = resumes.get(sid)
            if resumed and resumed > seen:
                # 用户在 ZCode 里直接打开过这条会话（session.resumed 日志）：算已读。
                # resume 时刻早于之后新回合的 updated，所以之后跑了新任务仍会重新提醒。
                seen_mark("zcode", sid, resumed)
                seen = resumed
            fresh = bool(updated) and (time.time() - updated / 1000) < 6 * 3600
            unread = (status in ("idle", "error")) and fresh and updated > seen
            result.append({"id": sid, "name": name[:40], "status": status,
                           "unread": unread,
                           "step": zcode_step(sid) if status in PINNED or unread else None, "source": "zcode",
                           "stamp": int(updated or 0), "path": directory})
        return result
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# ZCode 桌面端打开/切换会话会往 ~/.zcode/cli/log/zcode-<日期>.jsonl 写事件（带 sessionId）：
# 冷启动 resume 写 session.resumed，已驻留会话的切换有时只写 lifecycle_changed(state=starting)，
# 切走时是 lifecycle_changed stopping/unloaded。这是本地唯一的「用户此刻在看哪条会话」信号
# （无会话深链、无 CDP、窗口标题恒为「ZCode」、db 不记当前会话）：读到打开事件就把该会话
# 记为已读，与 WB 的 foreground session changed 事件同一套路（commit 41ebc8c）。
ZCODE_RESUMES = {}        # session_id -> 最近一次 resume 的事件时间(ms)
_ZCODE_LOG_STATE = {}     # 日志路径 -> 增量读取位置
_ZCODE_LOCK = threading.Lock()


def zcode_log_file():
    # 日志按天滚动（zcode-YYYY-MM-DD.jsonl），取最近写入的一份
    log_dir = Path(os.environ["USERPROFILE"]) / ".zcode" / "cli" / "log"
    try:
        files = sorted(log_dir.glob("zcode-*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return files[-1] if files else None


def _zcode_consume_locked(path):
    # 增量读日志，与 _wb_consume_locked 同构：只消费上次位置之后的新行，
    # 首次/轮转时从尾部 20MB 起读（只认当天的 resume 事件，够用且防大文件卡顿）。
    state = _ZCODE_LOG_STATE.get(str(path))
    try:
        size = path.stat().st_size
    except OSError:
        return
    if state is None or size < state["pos"]:
        state = {"pos": max(0, size - 20_000_000)}
        _ZCODE_LOG_STATE[str(path)] = state
    if size <= state["pos"]:
        return
    try:
        with path.open("rb") as handle:
            handle.seek(state["pos"])
            data = handle.read(size - state["pos"])
    except OSError:
        return
    cut = data.rfind(b"\n")
    if cut < 0:                      # 还没有完整的一行，下次再读
        return
    state["pos"] += cut + 1
    for line in data[:cut].decode("utf-8", "replace").splitlines():
        # 行过滤先行：绝大多数行与“用户打开会话”无关，避免整行 json 解析；
        # 桌面端打开会话有两种写法：冷启动 resume 写 session.resumed，已驻留会话
        # 的切换有时只写 lifecycle_changed(state=starting)（实测两种都出现过），
        # 是否真是打开事件以解析出的字段为准，不赌键值间的空格风格
        if "session.resumed" not in line and "lifecycle_changed" not in line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        event = payload.get("event")
        sid = None
        if event == "session.resumed":
            sid = payload.get("sessionId")
        elif event == "zcode_protocol.session.lifecycle_changed":
            context = payload.get("context")
            state = context.get("state") if isinstance(context, dict) else payload.get("state")
            if state == "starting":       # stopping/unloaded 是切走，不算打开
                sid = payload.get("sessionId")
        if sid:
            ZCODE_RESUMES[sid] = _log_ts_ms(line)


def zcode_resume_seen():
    # 消费最新日志，返回 {session_id: resume 时间ms}，供 zcode_sessions 记已读
    path = zcode_log_file()
    if path is not None:
        with _ZCODE_LOCK:
            _zcode_consume_locked(path)
    return dict(ZCODE_RESUMES)


def scnet_progress(session_path):
    status, step = None, None
    try:
        size = session_path.stat().st_size
        start = max(0, size - 200 * 1024)
        with session_path.open("rb") as stream:
            stream.seek(start)
            lines = stream.read(size - start).splitlines()
        if start:
            lines = lines[1:]
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if status is None:
                if role in ("user", "toolResult"):
                    status = "executing"
                elif role == "assistant":
                    status = {"stop": "idle", "error": "error", "aborted": "interrupted",
                              "toolUse": "executing"}.get(message.get("stopReason"), "unknown")
                    if status == "executing" and any(
                            isinstance(block, dict) and block.get("type") == "toolCall"
                            and block.get("name") in QUESTION_TOOLS
                            for block in message.get("content") or []):
                        status = "interactive"
                    if status == "error":
                        step = str(message.get("errorMessage") or "")[:90] or None
            if role != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and block.get("text"):
                    return status or "unknown", step or block["text"].strip().split("\n", 1)[0].strip()[:90]
                if block_type == "toolCall":
                    name = block.get("name")
                    if name:
                        return status or "executing", step or ("调用 " + name)[:90]
        return status or "unknown", step
    except (OSError, ValueError):
        return "unknown", None


def scnet_sessions():
    # SCNet（超算互联网）客户端会话：索引在 ~/.sclaw/agent/threads.json，
    # 每轮事件流在 sessions/<时间戳>_<uuid>.jsonl（与 Codex/ZCode 同源的 agent 框架）。
    # 本地无已读回执，从看板投递会话深链后记录该轮次已读。
    threads = Path(os.environ["USERPROFILE"]) / ".sclaw" / "agent" / "threads.json"
    if not threads.exists():
        return []
    try:
        data = json.loads(threads.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    threads_map = data.get("threads") if isinstance(data, dict) else None
    if not isinstance(threads_map, dict):
        return []
    result = []
    seen_cache = seen_load()
    now = time.time()
    for tid, info in threads_map.items():
        index = info.get("index") if isinstance(info, dict) else None
        if not isinstance(index, dict):
            continue
        title = index.get("title") or ""
        path = index.get("path") or ""
        updated = int(index.get("updatedAt") or 0)  # 毫秒
        failure = index.get("failure")
        if not isinstance(failure, dict):
            failure = None
        status, step = "unknown", None
        session_path = Path(path)
        if path and session_path.is_file():
            try:
                mtime = session_path.stat().st_mtime
                updated = max(updated, int(mtime * 1000))
                status, step = scnet_progress(session_path)
                if status == "executing" and now - mtime > 3600:
                    status = "unknown"  # 长时间静默只代表无法确认，不等于已完成。
            except OSError:
                pass
        if status == "unknown" and failure and failure.get("message"):
            status = "error"
            step = (failure.get("message") or "").strip().split("\n", 1)[0].strip()[:90] or step
        key = "scnet:" + tid
        seen = seen_cache.get(key, 0)
        fresh = bool(updated) and (now * 1000 - updated) < 6 * 3600 * 1000
        unread = (status not in ACTIVE) and (fresh or status in ("unknown", "interrupted")) and updated > seen
        name = title or "SCNet 会话"
        result.append({"id": tid, "name": name[:40], "status": status,
                       "unread": unread, "step": step, "source": "scnet", "stamp": int(updated or 0)})
    return result


def wb_user_dir():
    # WorkBuddy 的账号目录（~/.workbuddy/<uuid>），侧栏快照与路由状态都写在这里。
    base = Path(os.environ["USERPROFILE"]) / ".workbuddy"
    best, newest = None, 0.0
    try:
        for path in base.glob("*/sidebar-list-snapshot.json"):
            mtime = path.stat().st_mtime
            if mtime > newest:
                best, newest = path.parent, mtime
    except OSError:
        return None
    return best


# ── WorkBuddy 事件流 ──────────────────────────────────────────────────────
# workbuddy.db 的 sessions.status 在会话跑完后不归位（实测一直停在 working），
# 侧栏快照又只在状态变化时落盘，两者都会把“已经跑完”显示成“执行中”。
# 带时间戳的真实事件在日志里：
#   main.log   [TaskNotify] pending / completed / failed: session=<id>
#   daemon.log [MethodCh] begin wb:conversations:sendPrompt arg="<id>"   → 用户发消息，新一轮开始
#   daemon.log foreground session changed {"previous":..,"next":..}      → 用户此刻打开的是哪条会话
def wb_log_dir():
    # 每次调用再算，方便测试里改 USERPROFILE 指向临时目录。
    return Path(os.environ["USERPROFILE"]) / ".workbuddy" / "logs"
_NOTIFY_RE = re.compile(r"\[TaskNotify\] (pending|completed|failed): session=([0-9a-f-]{8,})")
_FOCUSED_RE = re.compile(r"wasFocused=(true|false)")
_SEND_PROMPT_RE = re.compile(r'sendPrompt arg=\\?"([0-9a-f-]{8,})')
_FOREGROUND_RE = re.compile(r'foreground session changed.*?"next"\s*:\s*"([0-9a-f-]{8,})"')


_TS_RE = re.compile(r'"timestamp"\s*:\s*"([^"\r\n]+)"')


def _log_ts_ms(line):
    match = _TS_RE.search(line)
    if not match:
        return 0
    try:
        stamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp() * 1000)


_WB_EVENT_STATE = {}
_WB_LOCK = threading.Lock()


def _wb_shared():
    # daemon.log / main.log 各自维护读取位置，但状态事件汇总到同一个字典里。
    shared = _WB_EVENT_STATE.get("__shared__")
    if shared is None:
        shared = {"sessions": {}, "active": "", "active_ts": 0, "seen": {}}
        _WB_EVENT_STATE["__shared__"] = shared
    return shared


def _wb_push(sid, kind, ts, focused=False):
    if not sid or not ts:
        return
    sessions = _wb_shared()["sessions"]
    old = sessions.get(sid)
    if old is None or ts >= old["ts"]:
        sessions[sid] = {"kind": kind, "ts": ts, "focused": focused}


def _wb_consume(path, is_daemon):
    # 采集线程与“点击后确认跳转”的后台线程都会读，加锁避免重复消费同一段日志。
    with _WB_LOCK:
        return _wb_consume_locked(path, is_daemon)


def _wb_consume_locked(path, is_daemon):
    # 增量读日志：只消费上次位置之后的新行（采集循环 2 秒一轮，不增量会反复解析几 MB）。
    # 首次运行 / 日志被轮转时整份读一次（上限 20MB），把当天的状态事件全部装进内存。
    key = str(path)
    state = _WB_EVENT_STATE.get(key)
    try:
        size = path.stat().st_size
    except OSError:
        return state
    if state is None or size < state["pos"]:
        # 首次（或日志被轮转）：从文件开头读，一次把当天所有状态事件装进内存，
        # 之后只消费新增行。限制 20MB 防止日志异常增大时卡住采集。
        state = {"pos": max(0, size - 20_000_000)}
        _WB_EVENT_STATE[key] = state
    if size <= state["pos"]:
        return state
    try:
        with path.open("rb") as handle:
            handle.seek(state["pos"])
            data = handle.read(size - state["pos"])
    except OSError:
        return state
    cut = data.rfind(b"\n")
    if cut < 0:                      # 还没有完整的一行，下次再读
        return state
    state["pos"] += cut + 1
    for line in data[:cut].decode("utf-8", "replace").splitlines():
        if is_daemon:
            match = _SEND_PROMPT_RE.search(line)
            if match:
                _wb_push(match.group(1), "prompt", _log_ts_ms(line))
                continue
            if "foreground session changed" in line:
                match = _FOREGROUND_RE.search(line)
                if match:
                    ts = _log_ts_ms(line)
                    shared = _wb_shared()
                    if ts >= shared["active_ts"]:
                        shared["active"], shared["active_ts"] = match.group(1), ts
                    sid = match.group(1)
                    shared["seen"][sid] = max(shared["seen"].get(sid, 0), ts)
        elif "[TaskNotify]" in line:
            match = _NOTIFY_RE.search(line)
            if not match:
                continue
            focus = _FOCUSED_RE.search(line)
            _wb_push(match.group(2), match.group(1), _log_ts_ms(line),
                     bool(focus and focus.group(1) == "true"))
    return state


def workbuddy_events():
    # 每个会话取“最后一条事件”，据此判断 在跑 / 待回复 / 完成 / 失败。
    # 先消费 daemon（sendPrompt=开始），再消费 main（completed/pending=结果）：
    # 时间戳保留毫秒；只有完全同时的事件才以结果为准。
    _wb_consume(wb_log_dir() / "daemon.log", True)
    _wb_consume(wb_log_dir() / "main.log", False)
    shared = _wb_shared()
    return {"sessions": dict(shared["sessions"]),
            "active": shared["active"],
            "active_ts": shared["active_ts"], "seen": dict(shared["seen"])}


def workbuddy_sidebar():
    # WorkBuddy 侧栏快照 = WB 自己 UI 里的会话状态：idle / working / pending / error。
    # pending 就是“等你回答或确认”，这是本地唯一能识别“待确认”的信号；
    # workbuddy.db 的 status 只有 working/completed/error，认不出等待用户的状态。
    folder = wb_user_dir()
    if folder is None:
        return {}
    try:
        data = json.loads((folder / "sidebar-list-snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    result = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        space = item.get("space") or {}
        result[item["id"]] = {
            "title": item.get("title") or "",
            "state": str(item.get("state") or "").lower(),
            "updated": int(item.get("lastActivityAt") or item.get("updatedAt") or 0),
            "space": ((space.get("name") if isinstance(space, dict) else "") or ""),
        }
    return result


def workbuddy_db_index():
    # workbuddy.db 的会话索引（标题/落库状态），只作兜底：它的 status 完成后不归位。
    db = Path(os.environ["USERPROFILE"]) / ".workbuddy" / "workbuddy.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT id, COALESCE(NULLIF(custom_title,''), title), status, updated_at "
            "FROM sessions WHERE deleted_at IS NULL "
            "ORDER BY CASE WHEN lower(COALESCE(status,'')) IN ('working','error') THEN 0 ELSE 1 END, "
            "updated_at DESC LIMIT 60").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {sid: {"title": title or "", "status": status or "", "updated": int(updated or 0)}
            for sid, title, status, updated in rows}


def workbuddy_sessions():
    # 状态以日志事件为准（sendPrompt=在跑 / pending=待回复 / completed / failed），
    # 没有事件的新会话才退回侧栏快照与 db。
    event_state = {"prompt": "executing", "pending": "interactive",
                   "completed": "idle", "failed": "error"}
    sidebar = workbuddy_sidebar()
    db_rows = workbuddy_db_index()
    events = workbuddy_events()
    event_map = events["sessions"]
    seen_cache = seen_load()
    now_ms = int(time.time() * 1000)
    result = []
    for sid in set(sidebar) | set(db_rows):
        info = sidebar.get(sid) or {}
        row = db_rows.get(sid) or {}
        # 侧栏快照的 working 不可信（实测早就 completed 的会话还挂着 working），
        # 只有 pending 是它独有的信号（db 里没有“等用户回答”这个状态），所以：
        # pending 用快照，其余一律以 db 落库状态为准，最外层再被日志事件覆盖。
        if info.get("state") == "pending":
            status = "interactive"
        else:
            status = ({"working": "executing", "completed": "idle", "Completed": "idle", "error": "error"}
                      .get(row.get("status", ""))
                      or {"working": "executing", "pending": "interactive", "idle": "idle", "error": "error"}
                      .get(info.get("state", "")))
        stamp = max(info.get("updated") or 0, row.get("updated") or 0)
        focused_seen = False
        event = event_map.get(sid)
        if event:
            mapped = event_state.get(event["kind"])
            # 发消息后卡住、日志里再没有结束事件的，超过 1 小时不再当作“执行中”
            if mapped == "executing" and now_ms - max(stamp, event["ts"]) > 3600 * 1000:
                mapped = "unknown"
            # db 仍标 working 时，比 db.updated_at 更早的终态事件属于上一轮的残留，
            # 不能把新一轮在跑的会话覆盖成「待命」。
            if (row.get("status") == "working" and mapped in ("idle", "error", "interactive")
                    and event["ts"] < (row.get("updated") or 0)):
                mapped = None
            if mapped:
                status = mapped
                stamp = max(stamp, event["ts"])
                focused_seen = event["kind"] in ("completed", "failed") and event["focused"]
        if status == "executing" and now_ms - stamp > 3600 * 1000:
            status = "unknown"
        if status is None:
            continue
        seen = seen_cache.get("workbuddy:" + sid, 0)
        receipt = events["seen"].get(sid, 0)
        if focused_seen:
            receipt = max(receipt, stamp)
        if receipt > seen:
            seen_mark("workbuddy", sid, receipt)
            seen = receipt
        fresh = bool(stamp) and now_ms - stamp < 6 * 3600 * 1000
        if status in ("idle", "error", "interactive", "unknown"):
            unread = (status == "interactive" or ((fresh or status == "unknown") and stamp > seen))
        else:
            unread = False
        # 完成那一刻用户就在这个会话里（wasFocused=true），WB 自己判定“已看过”，不必再闪
        if focused_seen:
            unread = False
        name = (info.get("title") or row.get("title")
                or (info.get("space") or "").replace("\\", "/").rsplit("/", 1)[-1]
                or "WorkBuddy 会话")
        result.append({"id": sid, "name": name[:40], "status": status, "unread": unread,
                       "step": None, "source": "workbuddy", "stamp": int(stamp or 0)})
    return result


class DailyTraffic:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        if not isinstance(self.state, dict):
            self.state = {}
        self.state = {key: entry for key, entry in self.state.items()
                      if isinstance(entry, dict) and self.valid_counters(entry.get("total"))}

    @staticmethod
    def valid_counters(value):
        return (isinstance(value, list) and len(value) == 2
                and all(isinstance(item, int) and item >= 0 for item in value))

    def update(self, key, counters):
        if counters is None:
            return None
        with self.lock:
            today = date.today().isoformat()
            entry = self.state.get(key, {})
            if entry.get("date") != today:
                entry = {"date": today, "total": [0, 0], "last": counters}
            elif self.valid_counters(entry.get("last")):
                entry["total"] = [total + (current - previous if current >= previous else current)
                                  for total, current, previous in zip(entry["total"], counters, entry["last"])]
            entry["last"] = counters
            self.state[key] = entry
            return entry["total"]

    def update_network(self, counters):
        if counters is None:
            return None
        with self.lock:
            entry = self.state.get("network", {})
            if entry.get("date") != date.today().isoformat():
                entry = {"date": date.today().isoformat(), "total": [0, 0]}
            previous = entry.get("interfaces")
            previous = previous if isinstance(previous, dict) else {}
            total = list(entry["total"])
            for name, current in counters.items():
                old = previous.get(name)
                if self.valid_counters(old):
                    for i in range(2):
                        total[i] += current[i] - old[i] if current[i] >= old[i] else current[i]
            # 旧版只有聚合 last：保留当天 total，首轮只建立逐网卡基线。
            self.state["network"] = {"date": entry["date"], "total": total, "interfaces": counters}
            return total

    def save(self):
        with self.lock:
            temp = STATE_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(self.state), encoding="utf-8")
            os.replace(temp, STATE_FILE)


_PHYSICAL_NICS = {"names": None, "ts": 0.0}


def physical_interfaces():
    now = time.monotonic()
    if now - _PHYSICAL_NICS["ts"] >= 60:
        service = None
        pythoncom.CoInitialize()
        try:
            # Win32_NetworkAdapter 的 PhysicalAdapter 在本机也把 VPN 隧道标成物理网卡。
            service = win32com.client.GetObject(r"winmgmts:\\.\root\StandardCimv2")
            names = {adapter.Name for adapter in service.ExecQuery(
                "SELECT Name FROM MSFT_NetAdapter WHERE HardwareInterface=True AND Virtual=False")
                if adapter.Name}
            _PHYSICAL_NICS["names"] = names
        except Exception:
            pass  # 查询失败时保留上次识别结果，首次失败则显示不可用。
        finally:
            service = None
            pythoncom.CoUninitialize()
        _PHYSICAL_NICS["ts"] = now
    return _PHYSICAL_NICS["names"]


class DailyBudget:
    def __init__(self):
        try:
            self.state = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        if not isinstance(self.state, dict) or not all(
                isinstance(self.state.get(key), (int, float)) and math.isfinite(self.state[key])
                for key in ("reset", "start", "used", "last_used", "budget")):
            self.state = {}

    def update(self, data, now=None):
        if not isinstance(data, dict):
            return None
        by_id = data.get("rateLimitsByLimitId")
        limits = (by_id.get("codex") if isinstance(by_id, dict) else None) or data.get("rateLimits") or {}
        if not isinstance(limits, dict):
            return None
        if "pro" not in str(limits.get("planType", "")):
            return None
        weekly = next((window for window in (limits.get("primary"), limits.get("secondary"))
                       if isinstance(window, dict) and isinstance(window.get("windowDurationMins"), (int, float))
                       and window["windowDurationMins"] >= 10080), None)
        if not weekly:
            return None
        now = time.time() if now is None else now
        try:
            reset, used = float(weekly["resetsAt"]), float(weekly["usedPercent"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(reset) or not math.isfinite(used) or reset <= now or used < 0:
            return None
        days = math.ceil((reset - now) / 86400)
        start = reset - days * 86400
        state = self.state
        if days == 7 and weekly["windowDurationMins"] == 10080 and (
                state.get("version") != 4 or state.get("pending") or
                (reset > state["reset"] + 86400 and used < state.get("last_used", used))):
            # 周重置后的第一天，周期起点的已用额度为零，可以直接回算。
            state = {"version": 2, "reset": reset, "start": start, "used": 0,
                     "budget": 100 / 7}
        elif state.get("version") not in (2, 3, 4):
            # 首次启用时缺少刷新点的用量基线，不能把当前周用量当作今天的起点。
            state = {"version": 2, "reset": reset, "start": start, "used": used,
                     "budget": 0, "pending": True}
        elif now >= state["start"] + 86400:
            # shortcut: 离线跨过刷新点时按恢复后的首个读数起算；有历史采样接口后可精确回填。
            # 每日节点一旦确定，不因接口 resetsAt 波动而提前或延后重新发放。
            start = state["start"] + math.floor((now - state["start"]) / 86400) * 86400
            allocation_reset = state["reset"] if state["reset"] > now else reset
            days = math.ceil((allocation_reset - now) / 86400)
            state = {"version": 2, "reset": allocation_reset, "start": start, "used": used,
                     "budget": max(0, 100 - used) / days}
        elif used < state.get("last_used", used):
            # 将恢复的额度分配到剩余天数，同时保留此前的日内消耗。
            allocation_reset = state["reset"] if state["reset"] > now else reset
            state["budget"] += (state["last_used"] - used) / math.ceil((allocation_reset - now) / 86400)
            state["used"] += used - state["last_used"]
        state["version"] = 4
        state["last_used"] = used
        state["observed_reset"] = reset
        state["sampled_at"] = now
        self.state = state
        try:
            temp = BUDGET_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temp, BUDGET_FILE)
        except OSError:
            pass
        return {"budget": None if state.get("pending") else state["budget"],
                "remaining": None if state.get("pending") else state["budget"] - (used - state["used"]),
                "resetsAt": state["start"] + 86400, "days": days}


class DiagnosticLog:
    """复制后截断原文件，保持 faulthandler 持有的描述符有效。"""
    def __init__(self, path, max_bytes=2 * 1024 * 1024):
        self.path, self.max_bytes = Path(path), max_bytes
        self.stream = self.path.open("a+", encoding="utf-8")
        self.lock = threading.RLock()

    def write(self, text):
        with self.lock:
            self.stream.flush()
            if os.fstat(self.stream.fileno()).st_size >= self.max_bytes:
                try:
                    temp = self.path.with_suffix(".tmp")
                    shutil.copyfile(self.path, temp)
                    os.replace(temp, self.path.with_name(self.path.stem + ".1.log"))
                    self.stream.seek(0)
                    self.stream.truncate()
                except OSError:
                    pass  # 备份失败时继续追加，不能先截断丢掉原日志。
            return self.stream.write(text)

    def flush(self):
        with self.lock:
            self.stream.flush()

    def fileno(self):
        return self.stream.fileno()

    def close(self):
        with self.lock:
            self.stream.close()


_diag = None


class RecycleInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("bytes", ctypes.c_longlong), ("items", ctypes.c_longlong)]


_RECYCLE = {"value": None, "ts": 0.0}


def recycle_size():
    # 回收站条目多时 SHQueryRecycleBinW 单次就要几百毫秒（实测 6500+ 条），每 2 秒查一次
    # 曾占面板 CPU 近半、还把采集轮拖到 7 秒一轮 —— 缓存 30 秒，显示上完全够新。
    now = time.monotonic()
    if now - _RECYCLE["ts"] < 30:
        return _RECYCLE["value"]
    info = RecycleInfo()
    info.size = ctypes.sizeof(info)
    value = info.bytes if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0 else None
    _RECYCLE.update(value=value, ts=now)
    return value


def codex_limits():
    desktop_bin = Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin"
    installed = [exe for exe in desktop_bin.glob("*/codex.exe") if exe.is_file()]
    command = (str(max(installed, key=lambda exe: exe.stat().st_mtime)) if installed
               else shutil.which("codex.exe") or shutil.which("codex.cmd"))
    if not command:
        return None
    # 使用可用的 Clash 混合端口。没有 Clash 时保留调用方的代理环境。
    env = dict(os.environ)
    try:
        cfg = yaml.safe_load(CLASH_CONFIG.read_text(encoding="utf-8")) or {}
        port = int(cfg.get("mixed-port") or 0)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        port = 0
    if port:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        proxy = f"http://127.0.0.1:{port}"
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    process = subprocess.Popen([command, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               creationflags=NO_WINDOW, env=env)
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
    threading.Thread(target=read, daemon=True).start()
    try:
        def request(message, request_id):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    response = messages.get(timeout=max(.1, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if response.get("id") == request_id:
                    return response.get("result")
            return None
        init = request({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "local_dashboard", "title": "Local Dashboard", "version": "1.0"}}}, 1)
        if init is None:
            return None
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        data = request({"id": 2, "method": "account/rateLimits/read"}, 2)
        if data and ((data.get("rateLimitsByLimitId") or {}).get("codex") or data.get("rateLimits")):
            return data
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def egress_lookup(use_proxy):
    # 多个出口地址接口依次尝试，任一返回 IP 即采用；优先带地理信息的接口，失败回退到仅返回 IP 的接口。
    # ipapi.co / ipinfo.io 免费额度很紧（面板每 5 分钟查一次也会被限到 429），所以把
    # 配额宽松、带中文地名的 ip-api 放最前，ipwho.is 作备选。
    candidates = [
        ("http://ip-api.com/json/?fields=status,country,city,query&lang=zh-CN", "geo-api"),
        ("https://ipwho.is/", "geo-api"),
        ("https://ipapi.co/json/", "geo"),
        ("https://ipinfo.io/json", "geo"),
        ("https://ifconfig.me/ip", "text"),
        ("https://api.ipify.org?format=json", "json_ip"),
    ]
    for url, kind in candidates:
        try:
            response = requests.get(url, timeout=8, proxies=use_proxy or {"http": "", "https": ""})
            response.raise_for_status()
            if kind == "geo-api":
                info = response.json()
                ip = info.get("query") or info.get("ip")
                place = " · ".join(filter(None, [info.get("city"), info.get("country")]))
            elif kind == "geo":
                info = response.json()
                ip = info.get("ip")
                place = " · ".join(filter(None, [info.get("city"),
                                                 info.get("country_name") or info.get("country")]))
            elif kind == "text":
                ip = response.text.strip()
                place = ""
            else:
                ip = response.json().get("ip")
                place = ""
            if ip:
                return ip, place
        except (ValueError, requests.RequestException):
            continue
    return None, None


def clash_config_request(method, **kwargs):
    config = yaml.safe_load(CLASH_CONFIG.read_text(encoding="utf-8"))
    controller = config["external-controller"]
    host = controller.rsplit(":", 1)[0].strip("[]")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Clash controller must be local")
    response = requests.request(method, "http://" + controller + "/configs",
        headers={"Authorization": "Bearer " + str(config.get("secret", ""))},
        timeout=2, proxies={"http": "", "https": ""}, **kwargs)
    response.raise_for_status()
    return response


def clash_tun_enabled():
    return clash_config_request("GET").json().get("tun", {}).get("enable")


def apply_clash_tun(enabled):
    clash_config_request("PATCH", json={"tun": {"enable": enabled}})
    return clash_tun_enabled() is enabled


def display_identity(screen):
    try:
        device = win32api.EnumDisplayDevices(screen.name(), 0, 1)
        if device.DeviceID:
            return device.DeviceID.casefold()
    except win32api.error:
        pass
    return screen.name().casefold()


def panel_display(screens, primary, selected):
    target = (next((screen for screen in screens if display_identity(screen) == selected), None)
              if selected else max((screen for screen in screens if screen != primary),
                                   key=lambda screen: screen.geometry().x(), default=None))
    if target is not None:
        area = target.geometry()
        if area.isValid() and not any(
                other != target and area.intersects(other.geometry()) for other in screens):
            return target
    return None


def proxy_info():
    try:
        response = clash_config_request("GET")
        current = response.json()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
        except FileNotFoundError:
            enabled = False
        tun = current.get("tun", {}).get("enable")
        if not enabled and not tun:
            return {"state": "未连接", "tun": bool(tun)}
        port = current.get("mixed-port")
        result = {"state": "已启用", "tun": bool(tun)}
        # TUN 模式下系统流量已被虚拟网卡接管，直连即可拿到出口地址；
        # 否则（系统代理模式）需显式走 mixed-port 代理。
        if tun:
            ip, place = egress_lookup(None)
            if ip is None and port:
                ip, place = egress_lookup(dict.fromkeys(("http", "https"), "http://127.0.0.1:" + str(int(port))))
        elif port:
            ip, place = egress_lookup(dict.fromkeys(("http", "https"), "http://127.0.0.1:" + str(int(port))))
        else:
            ip, place = None, None
        if ip:
            result.update({"ip": ip, "place": place or ""})  # 地名拿不到就留空，别拿 IP 顶着（会大小号重复显示）
        return result
    except (OSError, KeyError, ValueError, TypeError, AttributeError, requests.RequestException, yaml.YAMLError):
        return {"state": "状态不可用"}


_CPU_TEMP = {"value": None, "ts": 0.0}

# nvidia-smi 启动一次约 0.2s，每 2 秒 spawn 一次会长期占用单核近 10%，对看板意义有限；
# 缓存 10 秒，既保留 GPU 趋势又去掉这部分常驻开销（看板不需要秒级 GPU 精度）。
_GPU_CACHE = {"value": None, "ts": 0.0}


@lru_cache(maxsize=1)
def cpu_model():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except OSError:
        return ""


def cpu_temperature():
    # CPU 温度：优先 LibreHardwareMonitor / OpenHardwareMonitor 的 WMI 传感器（需对应软件以管理员
    # 权限运行），回退 ACPI 热区；结果缓存 30 秒，全部拿不到就返回 None（界面显示 —）。
    now = time.monotonic()
    if now - _CPU_TEMP["ts"] < 30:
        return _CPU_TEMP["value"]
    readings = []
    pythoncom.CoInitialize()
    try:
        for namespace, wql, field in (
            (r"root\LibreHardwareMonitor", "SELECT Name, Value FROM Sensor WHERE SensorType='Temperature'", "Value"),
            (r"root\OpenHardwareMonitor", "SELECT Name, Value FROM Sensor WHERE SensorType='Temperature'", "Value"),
            (r"root\wmi", "SELECT InstanceName, CurrentTemperature FROM MSAcpi_ThermalZoneTemperature", "CurrentTemperature"),
        ):
            try:
                service = win32com.client.GetObject("winmgmts:\\\\.\\" + namespace)
            except Exception:
                continue
            try:
                for sensor in service.ExecQuery(wql):
                    raw = getattr(sensor, field, None)
                    if not raw:
                        continue
                    celsius = float(raw) / 10 - 273.15 if field == "CurrentTemperature" else float(raw)
                    if -20 < celsius < 150:
                        name = getattr(sensor, "Name", "") or ""
                        if field == "CurrentTemperature" or "cpu" in name.lower() or "package" in name.lower():
                            readings.append((name, celsius))
            except Exception:
                continue
        if readings:
            packaged = [c for name, c in readings if "package" in name.lower() or "cpu" in name.lower()]
            value = round(packaged[0] if packaged else max(c for _, c in readings), 1)
        else:
            value = None
    finally:
        pythoncom.CoUninitialize()
    _CPU_TEMP.update(value=value, ts=now)
    return value


def deepseek_balance():
    # DeepSeek 开放平台余额：Key 取 deepseek.json（{"api_key": "sk-..."}）或环境变量 DEEPSEEK_API_KEY
    key = ""
    try:
        config = json.loads(DEEPSEEK_CONFIG.read_text(encoding="utf-8"))
        key = config.get("api_key", "") if isinstance(config, dict) else ""
    except (OSError, ValueError):
        pass
    key = (key if isinstance(key, str) else "") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return {"state": "未配置 Key"}
    try:
        response = requests.get("https://api.deepseek.com/user/balance",
            headers={"Authorization": "Bearer " + key}, timeout=8, proxies={"http": "", "https": ""})
        response.raise_for_status()
        data = response.json()
        info = (data.get("balance_infos") or [{}])[0]
        if not data.get("is_available", True):
            return {"state": "余额不可用"}
        return {"currency": info.get("currency", "CNY"), "total": info.get("total_balance"),
                "granted": info.get("granted_balance") or "0"}
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, requests.RequestException):
        return {"state": "读取失败，正在重试"}


class Collector(QtCore.QObject):
    updated = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.data = {"codex": None, "quota_state": "读取中", "proxy": {"state": "检测中"},
                     "sessions": None, "cpu_temp": None, "deepseek": {"state": "读取中"}}
        self.last_net = None
        self.lock = threading.Lock()
        try:
            self.keep_read_sessions = json.loads(READ_BEHAVIOR_CONFIG.read_text(encoding="utf-8")) is not False
        except (OSError, ValueError):
            self.keep_read_sessions = True
        self.daily = DailyTraffic()
        self.budget = DailyBudget()
        self.last_vpn = None
        self.tun_lock = threading.Lock()
        try:
            saved_tun = json.loads(TUN_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved_tun = None
        self.pending_tun = saved_tun if isinstance(saved_tun, bool) else None
        # 触发式刷新：点击前端刷新按钮时 set，让三个采集线程立即跑下一轮
        self._refresh = {name: threading.Event() for name in ("fast", "slow", "quotas")}

    def request_refresh(self):
        # 唤醒 fast / slow / quotas 三个线程，使其立刻重新采集并推送一次完整数据
        for event in self._refresh.values():
            event.set()

    def restore_tun(self):
        with self.tun_lock:
            enabled = self.pending_tun
            if enabled is None:
                return
            try:
                if clash_tun_enabled() is enabled or apply_clash_tun(enabled):
                    self.pending_tun = None
            except (OSError, KeyError, ValueError, TypeError, AttributeError, requests.RequestException, yaml.YAMLError):
                pass  # 开机时 Clash 可能比看板晚启动，下一轮继续尝试。

    def slow(self):
        while True:
            self._refresh["slow"].clear()
            self.restore_tun()
            value = proxy_info()
            with self.lock:
                self.data["proxy"] = value
            self._refresh["slow"].wait(10 if self.pending_tun is not None else 300)

    def quotas(self):
        next_balance = 0
        while True:
            refresh_requested = self._refresh["quotas"].is_set()
            self._refresh["quotas"].clear()
            try:
                value = None if os.environ.get("DASH_SKIP_QUOTA") else codex_limits()
            except (OSError, subprocess.SubprocessError):
                value = None
            daily_budget = self.budget.update(value) if value else None
            balance = None
            if refresh_requested or time.monotonic() >= next_balance:
                balance = deepseek_balance()
                next_balance = time.monotonic() + 60
            with self.lock:
                if value:
                    self.data["codex"] = value
                    self.data["daily_budget"] = daily_budget
                self.data["quota_state"] = "" if value else "读取失败，正在重试"
                if balance is not None:
                    self.data["deepseek"] = balance
            delay = min(15, max(1, daily_budget["resetsAt"] - time.time())) if daily_budget else 15
            self._refresh["quotas"].wait(delay if value else 10)

    def fast(self):
        psutil.cpu_percent(None)
        ticks = 0
        while True:
            self._refresh["fast"].clear()
            now = time.monotonic()
            try:
                interfaces = psutil.net_io_counters(pernic=True)
                names = physical_interfaces()
                counters = ({name: [stat.bytes_recv, stat.bytes_sent] for name, stat in interfaces.items()
                             if name in names} if names is not None else None)
                network_total = self.daily.update_network(counters)
                speeds = None
                if network_total is not None and self.last_net:
                    previous_time, previous_total = self.last_net
                    interval = now - previous_time
                    speeds = [max(0, current - old) * 8 / interval / 1e6
                              for current, old in zip(network_total, previous_total)]
                self.last_net = (now, network_total) if network_total is not None else None
                vpn_counters = clash_counters()
                vpn_speeds = None
                if vpn_counters and self.last_vpn:
                    old_time, old_values = self.last_vpn
                    vpn_speeds = [max(0, current - old) * 8 / (now - old_time) / 1e6
                                  for current, old in zip(vpn_counters, old_values)]
                self.last_vpn = (now, vpn_counters) if vpn_counters else None
                vpn_total = self.daily.update("vpn", vpn_counters)
                ticks += 1
                if ticks % 5 == 0:
                    try:
                        self.daily.save()
                    except OSError:
                        pass
                items, available = [], False
                for read_sessions in (unread_sessions, zcode_sessions, workbuddy_sessions, scnet_sessions, claude_sessions):
                    try:
                        items.extend(read_sessions())
                        available = True
                    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
                        continue
                # nvidia-smi 启动一次约 0.2s：缓存 10 秒再查，避免每 2 秒 spawn 一次拖慢整机。
                if now - _GPU_CACHE["ts"] >= 10:
                    value = None
                    try:
                        raw = subprocess.check_output(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total,name",
                            "--format=csv,noheader,nounits"], text=True, timeout=4, creationflags=NO_WINDOW)
                        usage, temperature, used, total, name = next(csv.reader(raw.splitlines(), skipinitialspace=True))
                        value = [float(usage), float(temperature), float(used), float(total), name]
                    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
                        pass
                    _GPU_CACHE["value"] = value
                    _GPU_CACHE["ts"] = now
                gpu = _GPU_CACHE["value"]
                disk = psutil.disk_usage(SYSTEM_DRIVE + "\\")
                memory = psutil.virtual_memory()
                cpu_temp = cpu_temperature()
                with self.lock:
                    sessions = cap_sessions(review_idle_sessions(
                        items, self.data["sessions"] or [], self.keep_read_sessions)) if available else None
                    self.data.update({"cpu": psutil.cpu_percent(None), "gpu": gpu,
                                      "cpu_temp": cpu_temp, "cpu_model": cpu_model(), "system_drive": SYSTEM_DRIVE,
                                      "memory": [memory.used, memory.total], "network": speeds,
                                      "network_total": network_total, "vpn": vpn_speeds, "vpn_total": vpn_total,
                                      "sessions": sessions, "disk": [disk.free, disk.total, disk.percent], "recycle": recycle_size()})
                    data = dict(self.data)
                self.updated.emit(json.dumps(data, ensure_ascii=False))
            except Exception:
                pass
            self._refresh["fast"].wait(2)


def _open_session_job(kind, session_id, title, idle_stamp=0):
    """点击条目后真正执行跳转，只允许在后台线程里跑。

    QWebChannel 的 slot 在 Qt 主线程执行；旧版除 codex 外都直接在 slot 里做窗口枚举、
    注册表写入、协议投递，一次 _focus_window 就是 4.5 秒，面板会整块失去响应。
    """
    try:
        if kind == "codex":
            # 优先按会话 ID 投递深链；协议不可用时才尝试 CDP。
            if _codex_open_session(session_id, title) and idle_stamp:
                seen_mark("reviewed:" + kind, session_id, idle_stamp)
        elif kind == "workbuddy":
            # 聚焦 + explorer 投递 workbuddy://chat/<id> 深链
            _open_workbuddy_session(session_id)
        elif kind == "zcode":
            # ZCode 的 app.asar 只有 oauth/callback 与 workspace/open 两条协议路由，
            # 没有会话级深链、也没开 CDP 端口：点击只能把客户端窗口恢复到前台，
            # 无法跳到具体会话（应用本身限制，不是面板漏做）。
            # ZCode.exe 同时是 zcode.cjs 的 node 运行时：无参数启动会拉起一个命令行
            # 窗口且不会切会话。所以只要进程还活着，绝不重复启动它。
            focused = _focus_window(_is_zcode_exe)
            if not focused and not _client_running("zcode"):
                exe = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "ZCode" / "ZCode.exe"
                if exe.exists():
                    subprocess.Popen([str(exe)], creationflags=NO_WINDOW)
        elif kind == "scnet":
            # SCNet 4.1.2 已在启动和 second-instance 中接收会话深链。
            if _SESSION_ID.fullmatch(session_id) and _open_url("scnet://threads/" + session_id):
                if idle_stamp:
                    seen_mark("reviewed:" + kind, session_id, idle_stamp)
                return
            focused = _focus_window(_is_scnet_exe)
            if not focused and not _client_running("计算服务"):
                exe = _scnet_exe_path()
                if exe:
                    subprocess.Popen([exe], creationflags=NO_WINDOW)
        elif kind == "claude":
            for item in claude_sessions():
                if item["id"] == session_id:
                    if claude_code.resume_session(session_id, item["path"]) and idle_stamp:
                        seen_mark("reviewed:" + kind, session_id, idle_stamp)
                    break
    except OSError:
        pass


class Bridge(QtCore.QObject):
    update = QtCore.pyqtSignal(str)
    tun_finished = QtCore.pyqtSignal(bool)

    def __init__(self, window, collector, web, tray):
        super().__init__()
        self.window = window
        self.collector = collector
        self.web = web
        self.tray = tray

    @QtCore.pyqtSlot(result=bool)
    def get_keep_read_sessions(self):
        with self.collector.lock:
            return self.collector.keep_read_sessions

    @QtCore.pyqtSlot(bool, result=bool)
    def set_keep_read_sessions(self, enabled):
        with self.collector.lock:
            try:
                temp = READ_BEHAVIOR_CONFIG.with_suffix(".tmp")
                temp.write_text(json.dumps(enabled), encoding="utf-8")
                os.replace(temp, READ_BEHAVIOR_CONFIG)
            except OSError:
                return False
            self.collector.keep_read_sessions = enabled
        self.collector.request_refresh()
        return True

    def prepare_screenshot(self, capture):
        # 等菜单隐藏后的合成帧完成，再抓取面板。
        self.web.page().runJavaScript("closeSettings()", lambda _: QtCore.QTimer.singleShot(100, capture))

    @QtCore.pyqtSlot()
    def copy_screenshot(self):
        def capture():
            try:
                pixmap = self.web.grab()
                if pixmap.isNull():
                    raise OSError("无法获取面板截图")
                QtWidgets.QApplication.clipboard().setPixmap(pixmap)
                self.tray.showMessage("截图已复制", "可直接粘贴到聊天或文档")
            except Exception as error:
                self.tray.showMessage("截图失败", str(error))
        self.prepare_screenshot(capture)

    @QtCore.pyqtSlot()
    def hide_panel(self):
        # 记下“用户主动收起”，屏幕增删触发的自动归位就不会把它又弹出来。
        self.window._user_hidden = True
        self.window.hide()

    @QtCore.pyqtSlot()
    def refresh(self):
        # 前端点击刷新按钮：立刻唤醒三个采集线程各跑一轮，覆盖硬件/网络/会话/额度/DeepSeek，
        # 采集结果经 bridge.update 推回前端，render 重绘所有区块（全局刷新）。
        self.collector.request_refresh()

    @QtCore.pyqtSlot(bool)
    def request_tun(self, enabled):
        # Clash 请求不能阻塞 Qt 主线程，否则点击后的圆心也要等网络返回才能绘制。
        def apply():
            self.tun_finished.emit(self.set_tun(enabled))
        threading.Thread(target=apply, daemon=True).start()

    @QtCore.pyqtSlot(bool, result=bool)
    def set_tun(self, enabled):
        try:
            with self.collector.tun_lock:
                if not apply_clash_tun(enabled):
                    return False
                self.collector.pending_tun = None
                with self.collector.lock:
                    self.collector.data["proxy"]["tun"] = enabled
                temp = TUN_STATE.with_suffix(".tmp")
                temp.write_text(json.dumps(enabled), encoding="utf-8")
                os.replace(temp, TUN_STATE)
                return True
        except (OSError, KeyError, ValueError, TypeError, AttributeError, requests.RequestException, yaml.YAMLError):
            return False
        finally:
            self.collector.request_refresh()

    @QtCore.pyqtSlot(result=str)
    def get_deepseek_key(self):
        # 返回当前已配置的 DeepSeek Key，用于预填“⋯”菜单里的输入框（仅本地面板可见）。
        try:
            data = json.loads(DEEPSEEK_CONFIG.read_text(encoding="utf-8"))
            key = data.get("api_key", "") if isinstance(data, dict) else ""
        except (OSError, ValueError):
            key = ""
        return (key if isinstance(key, str) else "") or os.environ.get("DEEPSEEK_API_KEY", "")

    @QtCore.pyqtSlot(str, result=bool)
    def set_deepseek_key(self, key):
        # 把用户在“⋯”菜单里填入的 DeepSeek Key 写入本地配置（deepseek.json），
        # 并立即触发一次刷新，让 DeepSeek 余额在面板上显示出来。
        try:
            try:
                data = json.loads(DEEPSEEK_CONFIG.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["api_key"] = key or ""
            temp = DEEPSEEK_CONFIG.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, DEEPSEEK_CONFIG)
            self.collector.request_refresh()  # 立即重采额度
            return True
        except OSError:
            return False

    @QtCore.pyqtSlot(bool, result=bool)
    def set_startup(self, enabled):
        previous = None
        tasks = []
        shortcut_checked = False
        try:
            tasks = [(task, task.Enabled) for task in startup_tasks()]
            previous = STARTUP.read_bytes() if STARTUP.exists() else None
            shortcut_checked = True
            if enabled:
                shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(STARTUP))
                shortcut.TargetPath = str(Path(sys.executable).with_name("pythonw.exe"))
                shortcut.Arguments = f'"{ROOT / "panel_watchdog.py"}"'
                shortcut.WorkingDirectory = str(ROOT)
                shortcut.Save()
            elif STARTUP.exists():
                STARTUP.unlink()
            for task, _ in tasks:
                task.Enabled = False
            return True
        except Exception:
            for task, was_enabled in tasks:
                try:
                    task.Enabled = was_enabled
                except Exception:
                    pass
            # 未开始修改快捷方式（查询任务失败）时，不做恢复写入。
            if shortcut_checked:
                try:
                    if previous is not None:
                        STARTUP.write_bytes(previous)
                    else:
                        STARTUP.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    @QtCore.pyqtSlot(str, str, str)
    def open_session(self, kind, session_id, title=""):
        # 点击条目只做「投递」：窗口枚举、注册表、协议投递、CDP 网络等待全部进后台线程。
        # QWebChannel 的 slot 跑在 Qt 主线程，旧版在这里做 _focus_window（4.5s）会让
        # 整个面板失去响应，也正是「点了没反应 / 卡死」的来源。
        # 重复点击由前端节流（1s）；这里不排队，用户快速改点另一条时后点的最后置前。
        if not session_id:
            return
        args = (kind, session_id, title)
        if kind in ("codex", "scnet", "claude"):
            with self.collector.lock:
                stamp = next((item["stamp"] for item in self.collector.data.get("sessions") or []
                              if item["source"] == kind and item["id"] == session_id
                              and item["status"] == "idle" and item["unread"]
                              and (kind != "codex" or "read_after" in item)), 0)
            if stamp:
                args += (stamp,)
        threading.Thread(target=_open_session_job, args=args,
                         daemon=True).start()

    @QtCore.pyqtSlot(str, str, float, result=bool)
    def mark_session_seen(self, kind, session_id, stamp):
        with self.collector.lock:
            item = next((item for item in self.collector.data.get("sessions") or []
                         if item["source"] == kind and item["id"] == session_id), None)
            if not item or stamp != item["stamp"] or stamp <= 0:
                return False
            if item["status"] == "idle":
                saved = seen_mark("dismissed:" + kind, session_id, stamp)
            elif (item["status"] in ("error", "unknown", "interrupted")
                  and (kind != "codex" or item["status"] == "interrupted")):
                saved = seen_mark(kind, session_id, stamp)
            else:
                return False
            if not saved:
                return False
        self.collector.request_refresh()
        return True

    @QtCore.pyqtSlot(result=bool)
    def startup_enabled(self):
        try:
            return STARTUP.exists() or any(task.Enabled for task in startup_tasks())
        except Exception:
            return STARTUP.exists()

    @QtCore.pyqtSlot(result=int)
    def get_brightness(self):
        # 返回上次保存的预览亮度（20-100），启动/刷新时前端据此恢复滑块与遮罩透明度。
        try:
            data = json.loads(BRIGHTNESS_CONFIG.read_text(encoding="utf-8"))
            v = int(data.get("value", 68)) if isinstance(data, dict) else 68
        except (OSError, ValueError, TypeError, OverflowError):
            v = 68
        return 20 if v < 20 else (100 if v > 100 else v)

    @QtCore.pyqtSlot(int, result=bool)
    def set_brightness(self, value):
        # 把“亮度”滑块选的值存到本地 brightness.json，下次启动自动恢复（拖动松手时调用）。
        try:
            v = 20 if value < 20 else (100 if value > 100 else int(value))
            temp = BRIGHTNESS_CONFIG.with_suffix(".tmp")
            temp.write_text(json.dumps({"value": v}, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, BRIGHTNESS_CONFIG)
            return True
        except OSError:
            return False


def main():
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    singleton = QtCore.QSharedMemory("local-monitor-dashboard-v1")
    if not singleton.create(1):
        return
    # codex:// 的 shell\open\command 在本机缺失（只剩 URL Protocol 声明），
    # 任何程序打开 codex:// 链接都会被当成 UNC 路径 "\\" 而弹错误框。
    # 启动时自检并补齐，Codex 升级换目录后也会自动跟着更新。
    if not DEEPSEEK_CONFIG.exists():
        DEEPSEEK_CONFIG.write_text('{\n  "api_key": ""\n}\n', encoding="utf-8")  # 填入 DeepSeek API Key 后显示余额
    window = QtWidgets.QMainWindow()
    # 置顶：面板没有任务栏图标也不在 Alt-Tab 里，一旦被别的窗口埋住就捞不上来。
    # 但 WindowStaysOnTopHint 在 Windows 上是靠 Qt **反复** SetWindowPos(HWND_TOPMOST) 断言的，
    # 每次数据刷新都可能顺带来一次整窗 z 序变更 → ScreenXpert 转发层重合成 → 闪两下。
    # 所以默认不用 Qt 的 flag，改为显示后由 Win32 只设一次 topmost（_force_topmost）。
    # 设 PANEL_QT_TOPMOST=1 可退回旧行为做对照实验。
    _qt_topmost = os.environ.get("PANEL_QT_TOPMOST") == "1"
    _flags = QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint
    if _qt_topmost:
        _flags |= QtCore.Qt.WindowStaysOnTopHint
    window.setWindowFlags(_flags)
    window.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)

    def _force_topmost():
        """把原生窗口设成 TOPMOST，**只设一次**，不参与每帧绘制。

        Qt 的 WindowStaysOnTopHint 会在窗口被激活/重绘时重新断言，副屏每次都要为此
        重新合成整块画面；直接调一次 SetWindowPos 就没有这个副作用。
        """
        if _qt_topmost:
            return
        try:
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_int
            HWND_TOPMOST = ctypes.wintypes.HWND(-1)
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            user32.SetWindowPos(ctypes.wintypes.HWND(int(window.winId())), HWND_TOPMOST,
                                0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:
            pass
    web = QtWebEngineWidgets.QWebEngineView()
    # QWebEngineView 默认是白底：副屏（ScreenPad）显示模式切换会重建 D3D 交换链，
    # 重建那一两帧还没来得及合成页面内容，露出的就是整屏白底。
    # 只设页面底色即可；不要再加 WA_OpaquePaintEvent —— 那会让 Qt 跳过清屏，
    # 合成面一旦丢失，窗口就变成透明（露出桌面壁纸），比白底更难排查。
    _BG = QtGui.QColor("#0d1217")
    web.page().setBackgroundColor(_BG)
    web.setStyleSheet("background:#0d1217;")
    window.setStyleSheet("QMainWindow{background:#0d1217;}")
    window.setAutoFillBackground(True)
    _palette = window.palette()
    _palette.setColor(window.backgroundRole(), _BG)
    window.setPalette(_palette)
    window.setCentralWidget(web)

    def _reset_zoom(*_):
        # 缩放被记住（历史 Ctrl+滚轮 / 换屏 DPI）会让内容超出窗口、下半部分被裁掉：
        # 加载前后都强制回到 1.0，并在加载完成后再校一次。
        try:
            web.setZoomFactor(1.0)
            web.page().settings().setAttribute(
                QtWebEngineWidgets.QWebEngineSettings.ZoomTextOnly, False)
        except Exception:
            pass

    _reset_zoom()
    web.loadFinished.connect(lambda ok: ok and _reset_zoom())
    QtCore.QTimer.singleShot(1200, _reset_zoom)

    # 整页变黑的两种可能：渲染进程崩溃（页面空白/黑）或页面被重载（闪一下再出内容）。
    # 这两条都落到诊断日志里，下次再黑就能直接判定是渲染层还是显示器层。
    def _log_event(text):
        try:
            _diag.write(text + "\n")
            _diag.flush()
        except Exception:
            pass
    web.loadStarted.connect(lambda: _log_event("LOAD_START"))
    web.loadFinished.connect(lambda ok: _log_event("LOAD_END ok=%s" % ok))

    collector = Collector()
    icon = QtGui.QIcon(str(ROOT / "panel-icon.ico"))
    if icon.isNull():
        icon = app.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
    tray = QtWidgets.QSystemTrayIcon(icon, app)
    tray.setToolTip("副屏看板")
    _render_failures = []

    def _render_gone(status, code):
        _log_event("RENDER_GONE status=%s code=%s" % (status, code))
        now = time.monotonic()
        _render_failures[:] = [stamp for stamp in _render_failures if now - stamp < 60]
        _render_failures.append(now)
        if len(_render_failures) <= 3:
            QtCore.QTimer.singleShot(len(_render_failures) * 1000, web.reload)
        else:
            tray.showMessage("看板页面未能恢复", "一分钟内多次崩溃，已停止自动重载。可从托盘重新加载页面。",
                             QtWidgets.QSystemTrayIcon.Warning)

    web.page().renderProcessTerminated.connect(_render_gone)
    bridge = Bridge(window, collector, web, tray)
    channel = QtWebChannel.QWebChannel(web.page())
    channel.registerObject("bridge", bridge)
    web.page().setWebChannel(channel)
    web.load(QtCore.QUrl.fromLocalFile(str(ROOT / "dashboard.html")))
    window.setWindowTitle("副屏看板")

    # 调试入口：设置环境变量 PANEL_SNAPSHOT=<png 路径> 启动，会在页面加载 4 秒后把
    # 面板自身渲染的位图存下来。物理屏上"看不见面板"时，用它区分是面板没画出来，
    # 还是画出来了但被 ScreenXpert 的合成层挡住（后者位图正常，屏幕内容却是别的东西）。
    _snap_path = os.environ.get("PANEL_SNAPSHOT")
    if _snap_path:
        def _auto_snapshot():
            try:
                if not web.grab().save(_snap_path):
                    raise OSError("无法保存截图：" + _snap_path)
                _log_event("SNAP saved " + _snap_path)
            except Exception as exc:
                _log_event("SNAP failed %r" % (exc,))
        QtCore.QTimer.singleShot(4000, _auto_snapshot)
    menu = QtWidgets.QMenu()
    show_action = menu.addAction("显示看板")
    hide_action = menu.addAction("收回看板")
    shot_action = menu.addAction("保存面板截图")
    copy_shot_action = menu.addAction("复制面板截图")
    copy_shot_action.triggered.connect(bridge.copy_screenshot)
    reload_action = menu.addAction("重新加载页面")
    reload_action.triggered.connect(lambda: (_render_failures.clear(), web.reload()))
    menu.addSeparator()
    quit_action = menu.addAction("退出")
    show_action.triggered.connect(lambda: _show_panel())
    hide_action.triggered.connect(bridge.hide_panel)
    def _quit_panel():
        try:
            STOP_FILE.touch()
        except OSError as error:
            tray.showMessage("退出失败", str(error), QtWidgets.QSystemTrayIcon.Warning)
            return
        app.quit()
    quit_action.triggered.connect(_quit_panel)

    def _save_shot():
        # 系统截图（Win+Shift+S 等）的全屏遮罩会盖住置顶面板，导致“截图时面板消失”。
        # 这里直接抓面板自身的渲染位图存盘，绕开截图工具的层级遮挡，是最稳妥的截图方式。
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = Path(os.path.expanduser("~/Desktop")) / f"副屏看板_{ts}.png"
            pix = web.grab()
            if not pix.save(str(out)):
                raise OSError("无法保存截图：" + str(out))
            try:
                _diag.write("SHOT saved " + str(out) + "\n")
                _diag.flush()
            except Exception:
                pass
            tray.showMessage("面板截图已保存", str(out), icon)
        except Exception as e:
            try:
                tray.showMessage("截图失败", str(e), icon)
            except Exception:
                pass

    shot_action.triggered.connect(lambda: bridge.prepare_screenshot(_save_shot))
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: _show_panel() if reason == QtWidgets.QSystemTrayIcon.DoubleClick else None)
    tray.show()

    try:
        selected_display = json.loads(DISPLAY_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        selected_display = None
    if not isinstance(selected_display, str):
        selected_display = None
    state = {"full_expected": False}

    def _save_display(identity):
        nonlocal selected_display
        selected_display = identity
        try:
            temp = DISPLAY_STATE.with_suffix(".tmp")
            temp.write_text(json.dumps(identity), encoding="utf-8")
            os.replace(temp, DISPLAY_STATE)
        except OSError:
            tray.showMessage("显示器设置保存失败", "本次运行仍会使用所选显示器。",
                             QtWidgets.QSystemTrayIcon.Warning)

    def _choose_display(identity):
        _save_display(identity)
        _show_panel()

    display_menu = menu.addMenu("显示器")

    def _list_displays():
        display_menu.clear()
        primary = app.primaryScreen()
        for screen in app.screens():
            identity = display_identity(screen)
            size = screen.size() * screen.devicePixelRatio()
            name = screen.model() or screen.name().rsplit("\\", 1)[-1]
            label = f"{name} · {size.width()} × {size.height()}"
            action = display_menu.addAction(label + (" · 主屏" if screen == primary else ""))
            action.setCheckable(True)
            action.setChecked(identity == selected_display)
            action.triggered.connect(lambda _, monitor=identity: _choose_display(monitor))

    display_menu.aboutToShow.connect(_list_displays)

    def _native_fullbleed_drift():
        """用物理像素校验窗口是否真的铺满所在显示器，没铺满返回 True。

        Qt5 混合 DPI（两块屏缩放比不同）下，窗口存活期间的 DPI 变化会走
        WM_DPICHANGED 处理，Qt 用错旧 DPR 把窗口尺寸改坏：副屏 125% 时实测
        2878x862 → 1799x539（恰为 120/192 折损，2026-11-13 唤醒后用户报
        "只剩一点"）。坏掉的窗口 geometry() 仍是"合法"的小矩形、中心也仍在
        副屏上，Qt 层面看不出来。进程本身是 Per-Monitor 感知的（实测
        awareness=2），GetWindowRect/EnumDisplayMonitors 给的都是物理像素，
        与 Qt 的逻辑坐标无关，直接和目标显示器的物理矩形比对最可靠。
        """
        try:
            user32 = ctypes.windll.user32
            rc = ctypes.wintypes.RECT()
            if not user32.GetWindowRect(int(window.winId()), ctypes.byref(rc)):
                return False
            mons = []
            proc = ctypes.WINFUNCTYPE(
                ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_void_p)

            def _cb(hmon, hdc, lprect, ldata):
                r = ctypes.cast(lprect, ctypes.POINTER(
                    ctypes.wintypes.RECT)).contents
                mons.append((r.left, r.top, r.right, r.bottom))
                return 1

            user32.EnumDisplayMonitors(0, 0, proc(_cb), 0)
            cx, cy = (rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2
            mon = next((m for m in mons
                        if m[0] <= cx < m[2] and m[1] <= cy < m[3]), None)
            if mon is None:
                return False
            if os.environ.get("PANEL_FULLBLEED") == "1":
                expect = mon
            else:
                expect = (mon[0] + 1, mon[1] + 1, mon[2] - 1, mon[3] - 1)
            native = (rc.left, rc.top, rc.right, rc.bottom)
            if any(abs(n - e) > 2 for n, e in zip(native, expect)):
                _log_event("NATIVE drift native=%s expect=%s" % (native, expect))
                return True
            return False
        except Exception:
            return False

    def _secondary():
        return panel_display(app.screens(), app.primaryScreen(), selected_display)

    def place_window():
        screens = app.screens()
        primary = app.primaryScreen()
        target = _secondary()
        if target is None:
            _log_event("PLACE secondary lost -> hide (screens=%d)" % len(screens))
            state["full_expected"] = False
            if window.isVisible():
                window.hide()
            return
        if selected_display is None:
            _save_display(display_identity(target))
        g = target.geometry()
        # **不要和输出尺寸完全一致**。窗口像素级铺满整块屏会满足 DWM 的 direct-flip
        # 全屏优化条件：之后每次内容更新都要退出再重新进入该模式，副屏就会「连闪两下」。
        # 这也解释了"把别的界面放上去就不闪"——它们铺不满整屏，压根不走这条路。
        # 四边各留 1px 就能破坏条件，深色壁纸下肉眼看不出来。PANEL_FULLBLEED=1 可恢复铺满。
        if os.environ.get("PANEL_FULLBLEED") != "1":
            g = g.adjusted(1, 1, -1, -1)
        _log_event("PLACE screens=%d primary=%s target=%s" % (
            len(screens),
            tuple(primary.geometry().getRect()) if primary else None,
            tuple(g.getRect())))
        state["full_expected"] = True
        if window.geometry() != g:
            window.setGeometry(g)
        # 摆放本身也可能被 Qt5 的 DPI 切换路径带偏（实测窗口在主副屏间搬动后
        # 回副屏，一次 setGeometry 会落成 1799x539，再摆一次才对）。
        # 显示后再校验一次，偏了就立刻重摆。
        if window.isVisible() and _native_fullbleed_drift():
            window.setGeometry(g)

    def _show_panel():
        # 托盘“显示看板”/双击：先归位再显示。直接 show() 会把窗口留在它被搬到的位置
        # （历史上副屏掉线时被搬到主屏，再 show 就铺满主屏）。
        window._user_hidden = False
        place_window()
        if state["full_expected"]:
            window.show()
            _force_topmost()

    def _ensure_on_screen():
        # 兜底处理没有发出 Qt 显示事件的 ScreenPad 恢复。
        if not window.isVisible():
            if not getattr(window, "_user_hidden", False) and _secondary() is not None:
                _relayout_now()
            return
        g = window.geometry()
        if g.width() <= 0 or g.height() <= 0:
            place_window()
            return
        cx, cy = g.x() + g.width() // 2, g.y() + g.height() // 2
        sec = _secondary()
        if sec is None:
            place_window()
            return
        if not sec.geometry().contains(cx, cy):
            # 副屏在，窗口却不在副屏上（被显示拓扑变化搬到主屏）：搬回来。
            _log_event("GUARD re-dock to secondary")
            place_window()
            return
        # 尺寸被 DPI 变化改坏的兜底：窗口中心仍在副屏上、geometry() 也是个
        # "合法"的小矩形，只有物理像素比对能看出来（见 _native_fullbleed_drift）。
        if state["full_expected"] and _native_fullbleed_drift():
            _log_event("GUARD native size drift -> re-place")
            place_window()
            return
        for screen in app.screens():
            if screen.geometry().contains(cx, cy):
                return
        _log_event("GUARD window offscreen -> place")
        place_window()

    _relayout = QtCore.QTimer()
    _relayout.setSingleShot(True)

    def _relayout_now():
        # 显示器增删带来的一连串事件做 1.2 秒防抖：每次 setGeometry 都是一次整窗重绘，
        # 拓扑抖动期间逐事件响应会把面板晃成一团。
        if _secondary() is not None:
            place_window()
            if not window.isVisible() and not getattr(window, "_user_hidden", False):
                window.show()          # 副屏回来了，把因掉线而隐藏的面板显示出来
                _force_topmost()
                # 显示后立刻再校验一次：唤醒/掉线恢复路径上 Qt 处理 DPI 变化
                # 会把尺寸改坏（见 _native_fullbleed_drift），这时窗口才可见、
                # 物理校验才跑得起来，1 秒级自愈而不是等 20 秒守护。
                place_window()
        else:
            place_window()             # 副屏又不在了：按 place_window 的规则隐藏

    _relayout.timeout.connect(_relayout_now)

    def _schedule_layout(*_):
        if window.isVisible() and _secondary() is None:
            window.hide()
        _relayout.start(1200)

    def _watch_screen(screen):
        screen.geometryChanged.connect(_schedule_layout)
        _schedule_layout()

    for screen in app.screens():
        screen.geometryChanged.connect(_schedule_layout)
    app.screenAdded.connect(_watch_screen)
    app.screenRemoved.connect(_schedule_layout)
    app.primaryScreenChanged.connect(_schedule_layout)

    # 启动即停靠；再延迟两次校正（捕捉副屏尚未就绪的竞态）；之后走 20 秒轻量守护 + 屏幕增删防抖。
    _show_panel()
    QtCore.QTimer.singleShot(1500, _relayout_now)
    QtCore.QTimer.singleShot(4000, _relayout_now)
    _guard = QtCore.QTimer()
    _guard.setInterval(20000)
    _guard.timeout.connect(_ensure_on_screen)
    _guard.start()

    # ScreenXpert 的显示切换遮罩（DisplayChangeMask）守卫 —— **默认关闭**。
    # 2026-11-12 实测：5 分钟里真正压制 0 次，却产生 74 次慢调用共 48.49 秒 ——
    # 它一次忙都没帮上，反过来每 4 秒让主线程卡 0.6 秒（用户体感「一卡一卡、连带闪两下」）。
    # 根因是「找不到遮罩就每次全量枚举 500+ 窗口」：没缓存住 hwnd 时成本极高。
    # 遮罩到底有没有挡路需要单独验证，不能靠定时全量枚举硬猜。
    # 需要时设 PANEL_MASK_GUARD=1 启动；即便如此也带退避：连续 3 次找不到就降到 5 秒一轮。
    if os.environ.get("PANEL_MASK_GUARD") == "1":
        _mask_state = [0]
        _mask_stats = [0, 0, 0.0]
        _mask_miss = [0]

        def _mask_guard():
            t0 = time.monotonic()
            try:
                if demote_display_mask(_mask_state):
                    _mask_stats[0] += 1
            finally:
                # 单次超过 30ms 说明又被某个窗口拖住了：记一笔，别让它静默变卡。
                cost = time.monotonic() - t0
                if cost > 0.03:
                    _mask_stats[2] += cost
                    _mask_stats[1] += 1
            if _mask_state[0]:
                _mask_miss[0] = 0
                if _mask_timer.interval() != 250:
                    _mask_timer.setInterval(250)
            else:
                _mask_miss[0] += 1
                if _mask_miss[0] >= 3 and _mask_timer.interval() != 5000:
                    _mask_timer.setInterval(5000)

        _mask_timer = QtCore.QTimer()
        _mask_timer.setInterval(250)
        _mask_timer.timeout.connect(_mask_guard)
        _mask_timer.start()

        def _mask_report():
            # 每 5 分钟汇报一次压制次数 / 慢调用，方便确认这个修复是否真在起作用
            if not (_mask_stats[0] or _mask_stats[1]):
                return
            try:
                _diag.write("MASK_DEMOTED %d 次/5min" % _mask_stats[0])
                if _mask_stats[1]:
                    _diag.write("，慢调用 %d 次共 %.2fs" % (_mask_stats[1], _mask_stats[2]))
                _diag.write("\n")
                _diag.flush()
            except Exception:
                pass
            _mask_stats[0] = _mask_stats[1] = 0
            _mask_stats[2] = 0.0
        _mask_log = QtCore.QTimer()
        _mask_log.setInterval(300000)
        _mask_log.timeout.connect(_mask_report)
        _mask_log.start()

    # 诊断心跳：每 5 秒写一行时间戳到 dash_diag.log，便于判断是 Python 异常崩还是被外部杀死
    def _heartbeat():
        try:
            _diag.write("HEARTBEAT " + datetime.now().isoformat(timespec="seconds") + "\n")
            _diag.flush()
        except Exception:
            pass
    _hb = QtCore.QTimer()
    _hb.setInterval(5000)
    _hb.timeout.connect(_heartbeat)
    _hb.start()
    _heartbeat()
    collector.updated.connect(bridge.update)
    app.aboutToQuit.connect(lambda: collector.daily.save())
    threading.Thread(target=collector.fast, daemon=True).start()
    threading.Thread(target=collector.slow, daemon=True).start()
    threading.Thread(target=collector.quotas, daemon=True).start()
    sys.exit(app.exec_())


if __name__ == "__main__":
    import faulthandler as _fh
    import traceback as _tb
    prepare_data()
    _diag = DiagnosticLog(DATA_DIR / "dash_diag.log")
    _fh.enable(file=_diag)
    sys.stderr = _diag  # pythonw 下 stderr 是 None，任何 print 都会崩；统一导到诊断文件
    def _excepthook(et, ev, tb):
        try:
            _tb.print_exception(et, ev, tb, file=_diag)
            _diag.flush()
        except Exception:
            pass
    sys.excepthook = _excepthook
    try:
        main()
    except Exception:
        _excepthook(*sys.exc_info())
        raise

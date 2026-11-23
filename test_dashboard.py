import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dashboard


class SessionProgressTest(unittest.TestCase):
    def test_live_turn_replaces_old_failure_and_stays_until_dismissed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            root = Path(temp) / ".codex"
            root.mkdir()
            rollout = root / "turn.jsonl"
            state = sqlite3.connect(root / "state_5.sqlite")
            with state:
                state.executescript("""
                    CREATE TABLE threads(id,name,title,rollout_path,archived,thread_source,updated_at);
                    CREATE TABLE thread_read_receipts(thread_id,first_unread_turn,last_published_turn,revision);
                    INSERT INTO thread_read_receipts VALUES ('thread','old','old',4);
                """)
                state.execute("INSERT INTO threads VALUES (?,?,?,?,0,'user',0)", ("thread", "", "测试会话", str(rollout)))
            with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as history, history:
                history.executescript("""
                    CREATE TABLE thread_turns(thread_id,turn_id,status,rollout_ordinal);
                    INSERT INTO thread_turns VALUES ('thread','old','failed',1);
                """)

            def append(kind, **fields):
                with rollout.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "event_msg", "payload": {"type": kind, "turn_id": "new", **fields}}) + "\n")

            try:
                append("task_started")
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "executing")
                self.assertAlmostEqual(dashboard.unread_sessions()[0]["stamp"],
                                       rollout.stat().st_mtime * 1000, delta=1)
                append("item_started", item={"type": "WebSearch"})
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "researching")
                append("item_started", item={"type": "FileChange"})
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "writing")
                append("item_started", item={"type": "McpToolCall"})
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "syncing")
                with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as history, history:
                    history.execute("INSERT INTO thread_turns VALUES ('thread','new','inProgress',2)")
                def response(**payload):
                    with rollout.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"type": "response_item", "payload": payload}) + "\n")
                response(type="function_call", name="request_user_input", call_id="q1",
                         arguments=json.dumps({"questions": [{"question": "选择哪种方案？"}]}))
                [question] = dashboard.unread_sessions()
                self.assertEqual((question["status"], question["step"]), ("interactive", "选择哪种方案？"))
                self.assertTrue(dashboard.review_idle_sessions([question], [])[0]["unread"])
                response(type="function_call_output", call_id="q1", output='{"answers": {}}')
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "executing")
                response(type="function_call", name="functions.request_user_input_async", call_id="q2",
                         arguments=json.dumps({"questions": [{"title": "是否继续？"}]}))
                response(type="function_call_output", call_id="q2", output='{"accepted": true}')
                response(type="message", role="assistant", content=[{"type": "output_text", "text": "继续检查"}])
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "interactive")
                response(type="message", role="user", content=[])
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "executing")
                with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as history, history:
                    history.execute("DELETE FROM thread_turns WHERE turn_id='new'")
                append("task_complete")
                pending = dashboard.unread_sessions()
                self.assertEqual(pending[0]["status"], "idle")
                self.assertTrue(pending[0]["unread"])
                self.assertEqual(pending[0]["receipt"], 4)
                with state:
                    state.execute("UPDATE thread_read_receipts SET first_unread_turn=NULL")
                items = dashboard.unread_sessions()
                self.assertEqual(dashboard.review_idle_sessions(items, []), [])
                read = dashboard.review_idle_sessions(items, pending)
                self.assertTrue(read[0]["read"])
                self.assertFalse(read[0]["unread"])
                self.assertEqual(dashboard.review_idle_sessions(items, []), read)
                self.assertEqual(dashboard.cap_sessions(read, limit=0), read)
                dashboard.seen_mark("dismissed:codex", "thread", read[0]["stamp"])
                self.assertEqual(dashboard.review_idle_sessions(items, read), [])
                # 日志尚未追上已落库的新回合时，仍以新回合的官方失败状态为准。
                with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as history, history:
                    history.execute("INSERT INTO thread_turns VALUES ('thread','new','completed',2)")
                    history.execute("INSERT INTO thread_turns VALUES ('thread','newer','failed',3)")
                self.assertEqual(dashboard.unread_sessions()[0]["status"], "error")
                rollout.unlink()
                with state:
                    state.execute("UPDATE threads SET updated_at=1234")
                    state.execute("UPDATE thread_read_receipts SET first_unread_turn='newer'")
                with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as history, history:
                    history.execute("UPDATE thread_turns SET status='completed' WHERE turn_id='newer'")
                self.assertEqual(dashboard.unread_sessions()[0]["stamp"], 1234000)
            finally:
                state.close()


class ZcodeStepTest(unittest.TestCase):
    def write_rollout(self, root, records):
        rollout = root / ".zcode" / "cli" / "rollout"
        rollout.mkdir(parents=True, exist_ok=True)
        (rollout / "model-io-sess.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8")

    def test_prefers_latest_thinking_from_request_messages(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            self.write_rollout(Path(temp), [{"request": {"messagesKind": "full", "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "reasoning", "text": "先检查日志结构\n再决定截断规则"},
                    {"type": "text", "text": "结论是继续"},
                ], "toolCalls": [{"name": "exec"}]},
            ]}}])
            self.assertEqual(dashboard.zcode_step("sess"), "先检查日志结构")

    def test_falls_back_to_text_tool_and_body_messages(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            self.write_rollout(Path(temp), [{"request": {"body": {"messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "回复正文"}]},
            ]}}}])
            self.assertEqual(dashboard.zcode_step("sess"), "回复正文")
            self.write_rollout(Path(temp), [{"request": {"messages": [
                {"role": "assistant", "content": None, "toolCalls": [{"name": "read_file"}]},
            ]}}])
            self.assertEqual(dashboard.zcode_step("sess"), "调用 read_file")


class ZcodeSessionTest(unittest.TestCase):
    def test_question_tool_waits_until_answered(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"), \
             patch("dashboard.zcode_step", return_value=None), patch("dashboard.zcode_live", return_value=True):
            db = Path(temp) / ".zcode/cli/db/db.sqlite"
            db.parent.mkdir(parents=True)
            now = int(time.time() * 1000)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE session(id,title,directory,time_updated,time_archived);
                    CREATE TABLE turn_usage(session_id,status,started_at);
                    CREATE TABLE tool_usage(session_id,tool_name,status,started_at,completed_at);
                """)
                conn.execute("INSERT INTO session VALUES ('s1','询问','',?,NULL)", (now,))
                conn.execute("INSERT INTO tool_usage VALUES ('s1','AskUserQuestion','running',?,NULL)", (now,))
                conn.execute("INSERT INTO tool_usage VALUES ('s1','Bash','running',?,NULL)", (now+1,))
            self.assertEqual(dashboard.zcode_sessions()[0]["status"], "interactive")
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("UPDATE tool_usage SET status='completed', completed_at=? WHERE tool_name='AskUserQuestion'", (now+1,))
            self.assertEqual(dashboard.zcode_sessions()[0]["status"], "executing")

    def test_title_falls_back_to_directory_tail(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            db = Path(temp) / ".zcode" / "cli" / "db" / "db.sqlite"
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE session(id,title,directory,time_updated,time_archived);
                    CREATE TABLE turn_usage(session_id,status,started_at);
                """)
                conn.execute("INSERT INTO session VALUES ('s1','',?,1,NULL)", (r"C:\Users\x\Projects\panel",))
            with patch("dashboard.zcode_step") as read_step:
                items = dashboard.zcode_sessions()
                read_step.assert_not_called()
            self.assertEqual(items[0]["name"], "panel")

    def test_directory_emitted(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            db = Path(temp) / ".zcode" / "cli" / "db" / "db.sqlite"
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE session(id,title,directory,time_updated,time_archived);
                    CREATE TABLE turn_usage(session_id,status,started_at);
                """)
                conn.execute("INSERT INTO session VALUES ('s1','会话',?,1,NULL)",
                             (r"C:\Users\x\Projects\panel",))
            items = dashboard.zcode_sessions()
            self.assertEqual(items[0]["path"], r"C:\Users\x\Projects\panel")


class WorkbuddySessionTest(unittest.TestCase):
    def test_working_session_hides_project_name(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            db = Path(temp) / ".workbuddy" / "workbuddy.db"
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE sessions(id,custom_title,title,status,cwd,unread,deleted_at,updated_at);
                """)
                conn.execute("INSERT INTO sessions VALUES ('wb1','','部署看板','working',?,1,NULL,?)",
                             (r"C:\Users\x\Projects\panel", int(time.time() * 1000)))
            items = dashboard.workbuddy_sessions()
            self.assertEqual(items[0]["status"], "executing")
            self.assertEqual(items[0]["id"], "wb1")
            self.assertIsNone(items[0]["step"])
            self.assertNotIn("项目", json.dumps(items, ensure_ascii=False))


class ZcodeLiveTest(unittest.TestCase):
    def test_running_turn_detected_even_when_last_turn_completed(self):
        # turn_usage 只在轮次结束后落库：正在跑的一轮查不到记录，必须靠日志活跃度识别
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            db = Path(temp) / ".zcode" / "cli" / "db" / "db.sqlite"
            rollout = Path(temp) / ".zcode" / "cli" / "rollout"
            db.parent.mkdir(parents=True)
            rollout.mkdir(parents=True)
            now_ms = int(time.time() * 1000)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE session(id,title,directory,time_updated,time_archived);
                    CREATE TABLE turn_usage(session_id,status,started_at);
                """)
                conn.execute("INSERT INTO session VALUES ('live','进行中的会话','',?,NULL)", (now_ms,))
                conn.execute("INSERT INTO turn_usage VALUES ('live','completed',?)", (now_ms - 60000,))
            (rollout / "model-io-live.jsonl").write_text("{}\n", encoding="utf-8")
            # 日志刚被追加 → 正在跑
            self.assertEqual(dashboard.zcode_sessions()[0]["status"], "executing")
            # 日志久未更新且会话时间久远 → 按 turn_usage 判为 idle
            old = time.time() - 3600
            os.utime(rollout / "model-io-live.jsonl", (old, old))
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("UPDATE session SET time_updated=? WHERE id='live'", (int(old * 1000),))
            self.assertEqual(dashboard.zcode_sessions()[0]["status"], "idle")


class ScnetSessionTest(unittest.TestCase):
    """SCNet（超算互联网）会话读取：索引在 ~/.sclaw/agent/threads.json，
    事件流在 sessions/<时间戳>_<uuid>.jsonl；步骤取最近助手文字/工具调用，
    状态按 jsonl 近期是否被追加判定 executing/idle（与 ZCode 一致）。"""

    def _write(self, temp, records):
        agent = Path(temp) / ".sclaw" / "agent"
        agent.mkdir(parents=True)
        rollout = agent / "sessions" / "2026-01-01T00-00-00-000Z_s1.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        threads = {
            "version": 1,
            "threads": {"s1": {"origin": {"type": "interactive"}, "index": {
                "id": "s1", "title": "SCNet 测试会话", "path": str(rollout),
                "updatedAt": int(time.time() * 1000), "workspaceDir": str(agent / "workspace")}}},
        }
        (agent / "threads.json").write_text(json.dumps(threads), encoding="utf-8")
        return rollout

    def test_step_prefers_latest_assistant_text_or_tool(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            rollout = self._write(temp, [
                {"type": "message", "id": "1", "timestamp": "2026-01-01T00:00:00Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "thinking", "thinking": "内部思考"},
                     {"type": "text", "text": "最终结论：完成"}]}},
                {"type": "message", "id": "2", "timestamp": "2026-01-01T00:00:01Z",
                 "message": {"role": "user", "content": [{"type": "text", "text": "继续"}]}},
            ])
            self.assertEqual(dashboard.scnet_progress(rollout), ("executing", "最终结论：完成"))
            rollout.write_text(json.dumps({"type": "message", "id": "3", "timestamp": "t",
                "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                    {"type": "toolCall", "id": "c1", "name": "powershell", "arguments": {}}]}}) + "\n",
                encoding="utf-8")
            self.assertEqual(dashboard.scnet_progress(rollout), ("executing", "调用 powershell"))

    def test_explicit_completion_immediately_becomes_idle(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            rollout = self._write(temp, [{"type": "message", "id": "1", "timestamp": "t",
                "message": {"role": "assistant", "stopReason": "toolUse", "content": [{"type": "text", "text": "步骤"}]}}])
            items = dashboard.scnet_sessions()
            self.assertEqual(items[0]["source"], "scnet")
            self.assertEqual(items[0]["name"], "SCNet 测试会话")
            self.assertEqual(items[0]["status"], "executing")
            with rollout.open("a", encoding="utf-8") as stream:
                stream.write('\n' + json.dumps({"type": "message", "message": {
                    "role": "assistant", "stopReason": "stop", "content": []}}) + '\n')
            items = dashboard.scnet_sessions()
            self.assertEqual(items[0]["status"], "idle")
            self.assertTrue(items[0]["unread"])

    def test_failure_marks_error_with_message(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            rollout = self._write(temp, [{"type": "message", "id": "1", "timestamp": "t",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "步骤"}]}}])
            # 模拟“已结束（不 fresh）”且 threads.json 记录了 failure 的会话
            old = time.time() - 600
            os.utime(rollout, (old, old))
            agent = Path(temp) / ".sclaw" / "agent"
            threads = json.loads((agent / "threads.json").read_text(encoding="utf-8"))
            threads["threads"]["s1"]["index"]["failure"] = {
                "schemaVersion": 1, "code": "model.timeout", "operation": "chat.prompt",
                "message": "Request timed out.", "providerId": "scnet", "providerType": "timeout"}
            (agent / "threads.json").write_text(json.dumps(threads), encoding="utf-8")
            items = dashboard.scnet_sessions()
            self.assertEqual(items[0]["status"], "error")
            self.assertEqual(items[0]["step"], "Request timed out.")
            self.assertTrue(items[0]["unread"])

    def test_exe_path(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {
                "ProgramFiles": temp, "ProgramFiles(x86)": temp, "LOCALAPPDATA": temp}):
            self.assertIsNone(dashboard._scnet_exe_path())
            exe = Path(temp) / "scnet-client" / "计算服务.exe"
            exe.parent.mkdir()
            exe.touch()
            self.assertEqual(dashboard._scnet_exe_path(), str(exe))


class VisibilityTest(unittest.TestCase):
    def test_workbuddy_error_hides_once_read(self):
        read = {"status": "error", "unread": False, "source": "workbuddy"}
        unread = {"status": "error", "unread": True, "source": "workbuddy"}
        self.assertTrue(dashboard._session_visible(unread))
        self.assertFalse(dashboard._session_visible(read))
        self.assertTrue(dashboard._session_visible({"status": "executing", "unread": False, "source": "codex"}))
        self.assertFalse(dashboard._session_visible({"status": "idle", "unread": False, "source": "codex"}))


class DeepseekBalanceTest(unittest.TestCase):
    def test_unconfigured_key_reports_state(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), \
             patch("dashboard.DEEPSEEK_CONFIG", Path(temp) / "deepseek.json"):
            self.assertEqual(dashboard.deepseek_balance(), {"state": "未配置 Key"})

    def test_balance_parses_platform_response(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), \
             patch("dashboard.DEEPSEEK_CONFIG", Path(temp) / "deepseek.json"):
            (Path(temp) / "deepseek.json").write_text('{"api_key": "sk-test"}', encoding="utf-8")
            response = type("R", (), {"raise_for_status": lambda self: None,
                                      "json": lambda self: {"is_available": True, "balance_infos": [
                                          {"currency": "CNY", "total_balance": "42.50", "granted_balance": "5.00"}]}})()
            with patch("dashboard.requests.get", return_value=response) as get:
                self.assertEqual(dashboard.deepseek_balance(),
                                 {"currency": "CNY", "total": "42.50", "granted": "5.00"})
                self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")


class WorkbuddySidebarTest(unittest.TestCase):
    """侧栏快照是 WB 自己 UI 的状态：pending 必须显示为“待回复”，
    历史 startup-route 不代表当前正在查看。"""

    def build(self, temp, items, active=None):
        account = Path(temp) / ".workbuddy" / "u1"
        account.mkdir(parents=True)
        (account / "sidebar-list-snapshot.json").write_text(
            json.dumps({"version": 3, "savedAt": 0, "items": items}, ensure_ascii=False), encoding="utf-8")
        (account / "startup-route.json").write_text(
            json.dumps({"path": f"/conversation/{active}" if active else "/home", "savedAt": 0}), encoding="utf-8")

    def test_pending_maps_to_interactive_and_stays(self):
        now = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            self.build(temp, [
                {"id": "a", "title": "等我确认", "state": "pending", "lastActivityAt": now},
                {"id": "b", "title": "跑完了", "state": "idle", "lastActivityAt": now},
            ])
            rows = {row["id"]: row for row in dashboard.workbuddy_sessions()}
            self.assertEqual(rows["a"]["status"], "interactive")
            self.assertTrue(rows["a"]["unread"])
            self.assertEqual(rows["b"]["status"], "idle")
            self.assertTrue(rows["b"]["unread"])

    def test_stale_startup_route_does_not_count_as_read(self):
        now = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            self.build(temp, [{"id": "b", "title": "跑完了", "state": "idle", "lastActivityAt": now}], active="b")
            rows = dashboard.workbuddy_sessions()
            self.assertTrue(rows[0]["unread"])
            self.assertNotIn("workbuddy:b", dashboard.seen_load())


class WorkbuddyEventTest(unittest.TestCase):
    """db 的 sessions.status 跑完后不归位、侧栏快照也会卡在 working，
    所以状态必须以日志事件为准：completed → 完成，sendPrompt → 执行中。"""

    SID = "aaaaaaaa-1111-2222-3333-444444444444"

    def build(self, temp, sidebar_state, events):
        account = Path(temp) / ".workbuddy" / "u1"
        account.mkdir(parents=True)
        now = int(time.time() * 1000)
        (account / "sidebar-list-snapshot.json").write_text(json.dumps({"version": 3, "savedAt": now, "items": [
            {"id": self.SID, "title": "会话", "state": sidebar_state, "lastActivityAt": now}]}, ensure_ascii=False),
            encoding="utf-8")
        logs = Path(temp) / ".workbuddy" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        main_lines, daemon_lines = [], []
        base = datetime.now(timezone.utc)
        for index, kind in enumerate(events):
            # 事件按秒递增，模拟真实的先后顺序
            stamp = (base + timedelta(seconds=index)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if kind == "prompt":
                daemon_lines.append(json.dumps({"timestamp": stamp, "scope": "daemon-server:WBBridge",
                    "message": [f'[MethodCh] begin #1 wb:conversations:sendPrompt arg="{self.SID}"']}))
            elif kind == "foreground":
                daemon_lines.append(json.dumps({"timestamp": stamp, "scope": "daemon-server:window",
                    "message": ["[TencentDocsDocumentService] foreground session changed",
                                {"previous": "x", "next": self.SID}]}))
            else:
                main_lines.append(json.dumps({"timestamp": stamp, "scope": "main",
                    "message": [f"[TaskNotify] {kind}: session={self.SID} wasFocused=false sent=false"]}))
        (logs / "main.log").write_text("\n".join(main_lines) + "\n", encoding="utf-8")
        (logs / "daemon.log").write_text("\n".join(daemon_lines) + "\n", encoding="utf-8")

    def run_case(self, sidebar_state, events):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"), \
             patch("dashboard._WB_EVENT_STATE", {}):
            self.build(temp, sidebar_state, events)
            return dashboard.workbuddy_sessions()[0]

    def test_completed_event_overrides_stuck_working(self):
        # 快照还停在 working（WB 完成后不重写），但有 completed 事件 → 必须是完成态并提醒
        row = self.run_case("working", ["prompt", "completed"])
        self.assertEqual(row["status"], "idle")
        self.assertTrue(row["unread"])

    def test_prompt_after_completed_is_executing(self):
        row = self.run_case("idle", ["completed", "prompt"])
        self.assertEqual(row["status"], "executing")
        self.assertFalse(row["unread"])

    def test_pending_event_is_interactive(self):
        row = self.run_case("working", ["prompt", "pending"])
        self.assertEqual(row["status"], "interactive")
        self.assertTrue(row["unread"])

    def test_foreground_log_counts_as_read(self):
        # 用户手动在 WB 里切到这条会话（不是通过面板），条目也要立刻算已读
        row = self.run_case("working", ["completed", "foreground"])
        self.assertEqual(row["status"], "idle")
        self.assertFalse(row["unread"])


class CodexOpenSessionTest(unittest.TestCase):
    SID = "01a1d356-9f2f-7a62-859c-45d3d8e5f93b"

    def test_exact_thread_opens_without_debug_port_or_sidebar(self):
        with patch("dashboard._ensure_codex_protocol") as ensure, \
             patch("dashboard._open_url", return_value=True) as send, \
             patch("dashboard._focus_window") as focus, \
             patch("dashboard._codex_click_thread", return_value=False) as click:
            self.assertTrue(dashboard._codex_open_session(self.SID, "同名聊天"))
            ensure.assert_called_once()
            send.assert_called_once_with("codex://threads/" + self.SID)
            click.assert_not_called()
            focus.assert_not_called()

    def test_protocol_failure_falls_back_to_cdp(self):
        with patch("dashboard._ensure_codex_protocol"), \
             patch("dashboard._open_url", return_value=False), \
             patch("dashboard._focus_window"), \
             patch("dashboard._codex_click_thread", return_value=True) as click:
            self.assertTrue(dashboard._codex_open_session(self.SID, "标题"))
            click.assert_called_once_with(self.SID, "标题")

    def test_failed_navigation_returns_false_after_one_retry(self):
        with patch("dashboard._ensure_codex_protocol"), \
             patch("dashboard._open_url", return_value=False), \
             patch("dashboard._focus_window"), patch("dashboard.time.sleep"), \
             patch("dashboard._codex_click_thread", return_value=False) as click:
            self.assertFalse(dashboard._codex_open_session(self.SID, "标题"))
            self.assertEqual(click.call_count, 2)

    def test_invalid_id_cannot_invoke_other_deep_link_actions(self):
        with patch("dashboard._ensure_codex_protocol") as ensure, \
             patch("dashboard._open_url") as send, patch("dashboard._focus_window") as focus:
            for value in ("", "new", "new?prompt=hello", self.SID + "?prompt=hello"):
                self.assertFalse(dashboard._codex_open_session(value, "标题"))
            send.assert_not_called()
            ensure.assert_not_called()
            focus.assert_not_called()


class OpenSessionDispatchTest(unittest.TestCase):
    """点击跳转必须全部下放到后台线程。

    QWebChannel 的 slot 跑在 Qt 主线程，旧版在这里做窗口枚举（实测 4.5s）
    会让整个面板失去响应 —— 这就是「点了没反应 / 卡死」的来源。
    """

    def test_no_client_work_on_ui_thread(self):
        started = []

        class _RecordingThread:
            def __init__(self, target=None, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                started.append((self.target, self.args))

        def forbidden(*_args, **_kwargs):
            raise AssertionError("窗口枚举 / 协议投递不能在 Qt 主线程执行")

        with patch("dashboard.threading.Thread", _RecordingThread), \
             patch("dashboard._focus_window", side_effect=forbidden), \
             patch("dashboard._open_url", side_effect=forbidden), \
             patch("dashboard._codex_open_session", side_effect=forbidden), \
             patch("dashboard._open_workbuddy_session", side_effect=forbidden):
            collector = SimpleNamespace(lock=dashboard.threading.Lock(), data={"sessions": []})
            bridge = dashboard.Bridge(None, collector, None, None)
            for kind in ("codex", "workbuddy", "zcode", "scnet"):
                bridge.open_session(kind, "sid-1", "标题")
        self.assertEqual(len(started), 4)
        self.assertTrue(all(target is dashboard._open_session_job for target, _ in started))
        self.assertEqual(started[0][1], ("codex", "sid-1", "标题"))

    def test_job_routes_every_source(self):
        calls = []
        with patch("dashboard._codex_open_session", side_effect=lambda *a: calls.append("codex")), \
             patch("dashboard._open_workbuddy_session", side_effect=lambda *a: calls.append("workbuddy")), \
             patch("dashboard._focus_window", side_effect=lambda *a: calls.append("focus") or True), \
             patch("dashboard._open_url", side_effect=lambda u: calls.append(u)), \
             patch("dashboard.seen_mark"), \
             patch("dashboard.subprocess.Popen"):
            for kind in ("codex", "workbuddy", "zcode", "scnet"):
                dashboard._open_session_job(kind, "sid-1", "标题")
        self.assertIn("codex", calls)
        self.assertIn("workbuddy", calls)
        # Codex / WB 交给专用函数；非法 SCNet ID 和不支持的 ZCode 路由不投递。
        self.assertFalse(any(isinstance(c, str) and
                             c.startswith(("scnet://", "zcode://", "codex://")) for c in calls))
        self.assertEqual(calls.count("focus"), 2)  # zcode + scnet

    def test_scnet_uses_its_supported_thread_route_before_focusing(self):
        sid = "bd74d4a3-a13e-46cb-b22c-b8b7f9c4d067"
        with patch("dashboard._open_url", return_value=True) as send, \
             patch("dashboard._focus_window") as focus, patch("dashboard.subprocess.Popen") as launch, \
             patch("dashboard.seen_mark") as mark:
            dashboard._open_session_job("scnet", sid, "标题", 1234)
            send.assert_called_once_with("scnet://threads/" + sid)
            mark.assert_called_once_with("reviewed:scnet", sid, 1234)
            focus.assert_not_called()
            launch.assert_not_called()


class FastProcessPathTest(unittest.TestCase):
    """_process_exe_fast 取代 psutil.Process().exe()，是点击提速的关键。"""

    def test_resolves_current_process_with_normalized_path(self):
        path = dashboard._process_exe_fast(os.getpid())
        self.assertTrue(path)
        self.assertNotIn("\\", path)
        self.assertEqual(path, path.lower())

    def test_unknown_pid_is_empty_not_exception(self):
        self.assertEqual(dashboard._process_exe_fast(0), "")


class HarnessRegressionTest(unittest.TestCase):
    """2026-11-12：ZCode 点击弹命令窗 / 会话漏显的回归用例。"""

    def test_interactive_is_pinned(self):
        self.assertTrue(dashboard._session_visible({"status": "interactive", "unread": False}))
        self.assertIn("interactive", dashboard.PINNED)

    def test_active_sessions_survive_cap(self):
        items = ([{"status": "executing", "unread": False, "stamp": i} for i in range(12)]
                 + [{"status": "idle", "unread": True, "stamp": 999}])
        out = dashboard.cap_sessions([s for s in items if dashboard._session_visible(s)])
        self.assertEqual(len(out), 12)
        self.assertTrue(all(s["status"] == "executing" for s in out))

    def test_idle_still_capped(self):
        items = [{"status": "idle", "unread": True, "stamp": i} for i in range(30)]
        out = dashboard.cap_sessions(items)
        self.assertEqual(len(out), dashboard.MAX_SESSIONS)
        self.assertEqual(out[0]["stamp"], 29)

    def test_minimized_window_uses_restored_rect(self):
        import ctypes
        import types

        class _GetRect:
            def __call__(self, hwnd, rect):
                r = ctypes.cast(rect, ctypes.POINTER(ctypes.wintypes.RECT)).contents
                r.left, r.top, r.right, r.bottom = -16000, -16000, -15843, -15975
                return 1

        class _GetPlacement:
            def __call__(self, hwnd, placement):
                p = ctypes.cast(placement, ctypes.POINTER(dashboard._WindowPlacement)).contents
                p.rcNormalPosition.left, p.rcNormalPosition.top = 0, 0
                p.rcNormalPosition.right, p.rcNormalPosition.bottom = 1200, 900
                return 1

        fake = types.SimpleNamespace(GetWindowRect=_GetRect(), IsIconic=lambda hwnd: 1,
                                     GetWindowPlacement=_GetPlacement())
        with patch("dashboard.ctypes.windll") as windll:
            windll.user32 = fake
            self.assertEqual(dashboard._window_area(1), 1200 * 900)

    def test_zcode_focus_failure_never_relaunches_cli(self):
        # ZCode.exe 同时是 zcode.cjs 的 node 运行时：进程还在时重复启动会弹命令行窗口。
        with patch("dashboard._focus_window", return_value=False), \
             patch("dashboard._client_running", return_value=True), \
             patch("dashboard.subprocess.Popen") as popen, \
             patch("dashboard.seen_mark"):
            dashboard._open_session_job("zcode", "sid", "标题")
            popen.assert_not_called()


class WorkbuddyStaleEventTest(unittest.TestCase):
    """db 仍标 working 时，上一轮的 completed 事件不能把新一轮在跑的会话盖成待命。"""

    SID = "bbbbbbbb-1111-2222-3333-444444444444"

    def test_stale_completed_does_not_hide_working_session(self):
        now = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"), \
             patch("dashboard._WB_EVENT_STATE", {}):
            base = Path(temp) / ".workbuddy"
            base.mkdir()
            with closing(sqlite3.connect(base / "workbuddy.db")) as conn, conn:
                conn.execute("CREATE TABLE sessions(id,title,custom_title,status,updated_at,deleted_at)")
                conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,NULL)",
                             (self.SID, "正在跑的会话", None, "working", now))
            logs = base / "logs"
            logs.mkdir()
            stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            (logs / "main.log").write_text(json.dumps({
                "timestamp": stamp, "scope": "main",
                "message": [f"[TaskNotify] completed: session={self.SID} wasFocused=true sent=false"]}) + "\n",
                encoding="utf-8")
            (logs / "daemon.log").write_text("", encoding="utf-8")
            rows = {row["id"]: row for row in dashboard.workbuddy_sessions()}
            self.assertEqual(rows[self.SID]["status"], "executing")
            self.assertTrue(dashboard._session_visible(rows[self.SID]))


class DeepLinkSafetyTest(unittest.TestCase):
    """协议投递不能引爆「Windows 找不到 '\\' 文件」。"""

    def setUp(self):
        patches = [patch("dashboard.subprocess.Popen"),
                   patch("dashboard._diag_url"),
                   patch("dashboard._protocol_handler", return_value='"app.exe" "%1"')]
        self.popen, self.diag, self.handler = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def test_unhandled_schemes_are_never_delivered(self):
        for url in ("https://example.com", "unknown://threads/abc",
                    "zcode://workspace/open?path=C:/x"):
            self.assertFalse(dashboard._open_url(url))
        self.popen.assert_not_called()

    def test_workbuddy_is_delivered(self):
        self.assertTrue(dashboard._open_url("workbuddy://chat/abc"))
        self.popen.assert_called_once()
        self.assertEqual(self.popen.call_args[0][0][1], "workbuddy://chat/abc")

    def test_codex_exact_thread_is_delivered_through_windows_shell(self):
        url = "codex://threads/01a1d356-9f2f-7a62-859c-45d3d8e5f93b"
        self.assertTrue(dashboard._open_url(url))
        self.popen.assert_called_once_with(["explorer.exe", url], creationflags=dashboard.NO_WINDOW)

    def test_scnet_thread_is_delivered_through_windows_shell(self):
        url = "scnet://threads/01a1c94e-4939-72c7-9368-0f7aa40a4daa"
        self.assertTrue(dashboard._open_url(url))
        self.popen.assert_called_once_with(["explorer.exe", url], creationflags=dashboard.NO_WINDOW)

    def test_missing_command_blocks_even_allowed_scheme(self):
        self.handler.return_value = ""
        self.assertFalse(dashboard._open_url("workbuddy://chat/abc"))
        self.popen.assert_not_called()

    def test_codex_protocol_repair_writes_command(self):
        self.handler.return_value = ""
        fake = r"C:\Program Files\WindowsApps\OpenAI.Codex_1\app\ChatGPT.exe"
        with patch("dashboard._codex_exe_path", return_value=fake), \
             patch("dashboard.winreg.OpenKey", side_effect=OSError), \
             patch("dashboard.winreg.CreateKey", return_value=MagicMock()), \
             patch("dashboard.winreg.SetValueEx") as set_value, \
             patch("dashboard.winreg.CloseKey"):
            dashboard._ensure_codex_protocol()
        self.assertTrue(set_value.called)
        self.assertEqual(set_value.call_args[0][4], '"%s" "%%1"' % fake)

    def test_codex_protocol_preserves_registered_handler(self):
        self.handler.return_value = '"CodexLauncher.exe" "%1"'
        with patch("dashboard.winreg.CreateKey") as create_key:
            dashboard._ensure_codex_protocol()
        create_key.assert_not_called()


class WorkBuddyClickLoopTest(unittest.TestCase):
    """WB 点击不能形成「闪烁 → 反复点 → 反复二次拉起客户端」的循环。"""

    def setUp(self):
        dashboard._WB_LAST_CLICK.clear()

    def test_second_click_within_20s_is_swallowed(self):
        urls, marks = [], []
        with patch("dashboard._focus_window"), \
             patch("dashboard._open_url", side_effect=lambda url: urls.append(url) or True), \
             patch("dashboard.seen_mark", side_effect=lambda *a: marks.append(a)):
            dashboard._open_workbuddy_session("sid")
            dashboard._open_workbuddy_session("sid")
        self.assertEqual(len(urls), 1)
        self.assertEqual(len(marks), 0)

    def test_click_does_not_mark_seen_without_receipt(self):
        marks = []
        with patch("dashboard._focus_window"), patch("dashboard._open_url"), \
             patch("dashboard.seen_mark", side_effect=lambda *a: marks.append(a)):
            dashboard._open_workbuddy_session("sid2")
        self.assertEqual(marks, [])


class ZcodeResumeTest(unittest.TestCase):
    """ZCode 桌面端没有会话深链/CDP/当前会话回执，但打开会话都会写日志：
    冷启动是 session.resumed，已驻留会话的切换有时只有 lifecycle_changed(starting)。
    两种都算「用户直接打开了」：读到就记已读；之后新回合的 updated
    会大于该时间戳，仍会重新提醒。stopping/unloaded 是切走，不算。"""

    def build(self, temp, updated_ms, resume_offset_ms, kind="resumed"):
        db = Path(temp) / ".zcode" / "cli" / "db" / "db.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE session(id,title,directory,time_updated,time_archived);
                CREATE TABLE turn_usage(session_id,status,started_at);
            """)
            conn.execute("INSERT INTO session VALUES ('s1','会话',?,?,NULL)", (r"C:\x", updated_ms))
        log_dir = Path(temp) / ".zcode" / "cli" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp((updated_ms + resume_offset_ms) / 1000, tz=timezone.utc)
        line = {"timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "level": "info", "sessionId": "s1"}
        if kind == "resumed":
            line.update(event="session.resumed", message="Session resumed")
        else:
            line.update(event="zcode_protocol.session.lifecycle_changed", context={"state": kind})
        (log_dir / "zcode-2030-01-01.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    def run_case(self, updated_ms, resume_offset_ms, kind="resumed"):
        # 返回 (条目, seen缓存)：已读断言必须在这个补丁块内做，块外 dashboard.SEEN_FILE
        # 已恢复为真实文件（WB 的 test_open_conversation_counts_as_read 同理）。
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"), \
             patch("dashboard._ZCODE_LOG_STATE", {}), \
             patch("dashboard.ZCODE_RESUMES", {}):
            self.build(temp, updated_ms, resume_offset_ms, kind)
            rows = dashboard.zcode_sessions()
            return (rows[0] if rows else None), dashboard.seen_load()

    def test_resume_log_marks_read(self):
        # updated 在 2 分钟前（fresh 且非 live），1 分钟前直接打开过 → 已读
        now = time.time() * 1000
        row, seen = self.run_case(now - 120_000, 60_000)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "idle")
        self.assertFalse(row["unread"])
        self.assertGreater(seen.get("zcode:s1", 0), now - 120_000)

    def test_new_turn_after_resume_rearms(self):
        # 打开之后又跑了新回合（updated 晚于 resume）→ 重新提醒
        now = time.time() * 1000
        row, _ = self.run_case(now - 120_000, -150_000)
        self.assertEqual(row["status"], "idle")
        self.assertTrue(row["unread"])

    def test_lifecycle_starting_also_marks_read(self):
        # 已驻留会话的切换只写 lifecycle_changed(starting)、不写 resumed → 同样算已读
        now = time.time() * 1000
        row, seen = self.run_case(now - 120_000, 60_000, kind="starting")
        self.assertEqual(row["status"], "idle")
        self.assertFalse(row["unread"])
        self.assertGreater(seen.get("zcode:s1", 0), now - 120_000)

    def test_lifecycle_stopping_does_not_mark(self):
        # stopping 是切走不是打开 → 仍提醒
        now = time.time() * 1000
        row, seen = self.run_case(now - 120_000, 60_000, kind="stopping")
        self.assertTrue(row["unread"])
        self.assertNotIn("zcode:s1", seen)

    def test_without_log_still_unread(self):
        # 没有任何 resume 日志（如面板首次运行）：维持原有提醒行为
        now = time.time() * 1000
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"), \
             patch("dashboard._ZCODE_LOG_STATE", {}), \
             patch("dashboard.ZCODE_RESUMES", {}):
            self.build(temp, now - 120_000, 0)
            (Path(temp) / ".zcode" / "cli" / "log" / "zcode-2030-01-01.jsonl").write_text("", encoding="utf-8")
            rows = dashboard.zcode_sessions()
            self.assertTrue(rows[0]["unread"])
            self.assertFalse(dashboard.seen_load())


class ZcodeStatusPriorityTest(unittest.TestCase):
    """实时信号（model-io / 仍在跑的工具）必须压过历史 error——上一轮报错、
    新一轮正在跑的会话要显示执行中而不是异常；长工具静默期也不能闪回「待命」。"""

    def build(self, temp, updated_ms, turn, tools):
        # turn: (status, completed_ms)；tools: [(name, status, started_ms, completed_ms)]
        db = Path(temp) / ".zcode" / "cli" / "db" / "db.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE session(id,title,directory,time_updated,time_archived);
                CREATE TABLE turn_usage(session_id,status,started_at,completed_at);
                CREATE TABLE tool_usage(session_id,tool_name,status,started_at,completed_at);
            """)
            conn.execute("INSERT INTO session VALUES ('s1','会话',?,?,NULL)", (r"C:\x", updated_ms))
            if turn:
                conn.execute("INSERT INTO turn_usage VALUES (?,?,?,?)",
                             ('s1', turn[0], turn[1] - 60_000, turn[1]))
            for name, tool_status, started, done in tools:
                conn.execute("INSERT INTO tool_usage VALUES ('s1',?,?,?,?)",
                             (name, tool_status, started, done))

    def run_case(self, updated_ms, turn, tools):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"), \
             patch("dashboard._ZCODE_LOG_STATE", {}), \
             patch("dashboard.ZCODE_RESUMES", {}):
            self.build(temp, updated_ms, turn, tools)
            rows = dashboard.zcode_sessions()
            return rows[0]["status"] if rows else None

    def test_stale_error_does_not_shadow_running_turn(self):
        # 上一轮 74 分钟前 error，新一轮正在跑（工具 running、会话刚更新）→ 执行中而非异常
        now = time.time() * 1000
        status = self.run_case(now - 3_000, ("error", now - 74 * 60_000),
                               [("Bash", "running", now - 61_000, None)])
        self.assertEqual(status, "executing")

    def test_fresh_error_still_shows_error(self):
        # error 刚落下（90 秒内）且无任何实时信号 → 仍显示异常
        now = time.time() * 1000
        status = self.run_case(now - 5_000, ("error", now - 5_000), [])
        self.assertEqual(status, "error")

    def test_new_tool_after_recent_failure_is_executing(self):
        now = time.time() * 1000
        status = self.run_case(now - 1000, ("error", now - 5000),
                               [("Bash", "running", now - 1000, None)])
        self.assertEqual(status, "executing")

    def test_long_tool_keeps_executing(self):
        # 工具已跑 6 分钟（model-io 静默远超 300s 窗口、会话行 10 分钟没动）→ 仍执行中
        now = time.time() * 1000
        status = self.run_case(now - 10 * 60_000, ("completed", now - 10 * 60_000),
                               [("Bash", "running", now - 6 * 60_000, None)])
        self.assertEqual(status, "executing")

    def test_zombie_running_tool_does_not_pin_executing(self):
        # 进程被硬杀遗留的 running 行（早于上一轮结束时刻）→ 不能永远钉在执行中
        now = time.time() * 1000
        status = self.run_case(now - 10 * 60_000, ("completed", now - 10 * 60_000),
                               [("Bash", "running", now - 2 * 3600_000, None)])
        self.assertEqual(status, "idle")


if __name__ == "__main__":
    unittest.main()

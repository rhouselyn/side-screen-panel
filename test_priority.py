import faulthandler
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dashboard
import panel_watchdog
import test_audit


def quota(used, reset):
    return {"rateLimits": {"planType": "pro", "primary": {
        "usedPercent": used, "resetsAt": reset, "windowDurationMins": 10080}}}


class PriorityRegressionTest(unittest.TestCase):
    def test_daily_budget_spending_restart_and_next_reset_hour(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.BUDGET_FILE", Path(temp) / "quota.json"):
            reset = 2_000_000_000
            now = reset - 4 * 86400 + 3600
            budget = dashboard.DailyBudget()
            budget.state = {"version": 2, "reset": reset, "start": reset - 5 * 86400,
                            "used": 60, "last_used": 60, "budget": 10}
            self.assertEqual(budget.update(quota(60, reset), now)["remaining"], 10)
            result = budget.update(quota(66, reset), now + 100)
            self.assertEqual(result["budget"], 10)
            self.assertEqual(result["remaining"], 4)
            self.assertEqual(result["days"], 4)
            self.assertEqual(result["resetsAt"], reset - 3 * 86400)
            budget = dashboard.DailyBudget()
            self.assertEqual(budget.update(quota(69, reset), now + 200)["remaining"], 1)
            result = budget.update(quota(69, reset), reset - 3 * 86400)
            self.assertAlmostEqual(result["budget"], 31 / 3)
            self.assertAlmostEqual(result["remaining"], 31 / 3)

    def test_budget_overrun_week_rollover_credit_reset_and_bad_data(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.BUDGET_FILE", Path(temp) / "quota.json"):
            reset = 2_000_000_000
            now = reset - 4 * 86400
            budget = dashboard.DailyBudget()
            budget.state = {"version": 2, "reset": reset, "start": reset - 5 * 86400,
                            "used": 60, "last_used": 60, "budget": 10}
            budget.update(quota(60, reset), now)
            self.assertEqual(budget.update(quota(75, reset), now + 1)["remaining"], -5)
            result = budget.update(quota(20, reset), now + 2)
            self.assertAlmostEqual(result["budget"], 10 + 55 / 4)
            self.assertAlmostEqual(result["remaining"], -5 + 55 / 4)
            self.assertAlmostEqual(dashboard.DailyBudget().update(quota(21, reset), now + 3)["remaining"], -6 + 55 / 4)
            self.assertAlmostEqual(budget.update(quota(0, reset + 7 * 86400), reset)["remaining"], 100 / 7)
            self.assertIsNone(budget.update(quota(0, reset), reset))
            self.assertIsNone(budget.update(quota(float("nan"), reset), now))
            self.assertIsNone(budget.update({"rateLimits": {"planType": "plus"}}, now))
            # 时钟回退不能触发重新分配。
            self.assertAlmostEqual(budget.update(quota(100, reset), now)["budget"], 100 / 7)
            dashboard.BUDGET_FILE.write_text('{"budget":"bad"}', encoding="utf-8")
            self.assertIsNone(dashboard.DailyBudget().update(quota(60, reset), now)["remaining"])

    def test_reset_card_starts_new_weekly_budget(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.BUDGET_FILE", Path(temp) / "quota.json"):
            now = 2_000_000_000
            old_reset = now + 3 * 86400
            new_reset = now + 7 * 86400 - 60
            budget = dashboard.DailyBudget()
            budget.state = {"version": 4, "reset": old_reset, "start": now - 3600,
                            "used": 60, "last_used": 90, "budget": 10}
            result = budget.update(quota(0, new_reset), now)
            self.assertAlmostEqual(result["budget"], 100 / 7)
            self.assertAlmostEqual(result["remaining"], 100 / 7)
            self.assertAlmostEqual(dashboard.DailyBudget().update(quota(1, new_reset), now + 1)["remaining"], 100 / 7 - 1)

    def test_first_sample_and_old_state_wait_for_daily_boundary(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.BUDGET_FILE", Path(temp) / "quota.json"):
            reset = 2_000_000_000
            now = reset - 4 * 86400 + 3600
            dashboard.BUDGET_FILE.write_text(json.dumps({"reset": reset, "start": reset - 4 * 86400,
                "used": 20, "last_used": 20, "budget": 80 / 4}))
            budget = dashboard.DailyBudget()
            self.assertIsNone(budget.update(quota(20, reset), now)["remaining"])
            self.assertIsNone(dashboard.DailyBudget().update(quota(25, reset), now + 100)["remaining"])
            result = dashboard.DailyBudget().update(quota(25, reset), reset - 3 * 86400)
            self.assertEqual(result["remaining"], 25)
            self.assertEqual(dashboard.DailyBudget().update(quota(30, reset), reset - 3 * 86400 + 1)["remaining"], 20)

    def test_first_week_day_recovers_from_weekly_reset(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.BUDGET_FILE", Path(temp) / "quota.json"):
            reset = 2_000_000_000
            now = reset - 7 * 86400 + 3600
            result = dashboard.DailyBudget().update(quota(20, reset), now)
            self.assertAlmostEqual(result["remaining"], 100 / 7 - 20)
            self.assertAlmostEqual(dashboard.DailyBudget().update(quota(22, reset), now + 1)["remaining"], 100 / 7 - 22)
            dashboard.BUDGET_FILE.write_text(json.dumps({"version": 2, "reset": reset,
                "start": reset - 7 * 86400, "used": 20, "last_used": 20, "budget": 0, "pending": True}))
            self.assertAlmostEqual(dashboard.DailyBudget().update(quota(22, reset), now + 2)["remaining"], 100 / 7 - 22)
            dashboard.BUDGET_FILE.write_text(json.dumps({"version": 2, "reset": reset,
                "start": reset - 7 * 86400, "used": 20, "last_used": 20, "budget": 80 / 7}))
            self.assertAlmostEqual(dashboard.DailyBudget().update(quota(22, reset), now + 3)["remaining"], 100 / 7 - 22)
            self.assertAlmostEqual(dashboard.DailyBudget().update(quota(23, reset), now + 4)["remaining"], 100 / 7 - 23)

    def test_reset_timestamp_changes_never_reallocate_before_saved_boundary(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.BUDGET_FILE", Path(temp) / "quota.json"):
            reset = 2_000_000_000
            now = reset - 7 * 86400 + 3600
            budget = dashboard.DailyBudget()
            first = budget.update(quota(20, reset), now)
            boundary = first["resetsAt"]
            for offset, used in ((1, 21), (-1, 22), (3600, 23), (0, 24)):
                budget = dashboard.DailyBudget()
                result = budget.update(quota(used, reset + offset), now + used)
                self.assertAlmostEqual(result["budget"], 100 / 7)
                self.assertAlmostEqual(result["remaining"], 100 / 7 - used)
                self.assertEqual(result["resetsAt"], boundary)
            result = budget.update(quota(25, reset + 1), boundary)
            self.assertAlmostEqual(result["remaining"], 75 / 6)
            self.assertAlmostEqual(budget.update(quota(26, reset + 1), boundary + 1)["remaining"], 75 / 6 - 1)

    def test_repairs_persisted_version_three_false_allocation(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.BUDGET_FILE", Path(temp) / "quota.json"):
            reset = 2_000_000_000
            dashboard.BUDGET_FILE.write_text(json.dumps({"version": 3, "reset": reset,
                "start": reset - 7 * 86400, "used": 21, "last_used": 22, "budget": 79 / 7}))
            result = dashboard.DailyBudget().update(quota(22, reset), reset - 7 * 86400 + 3600)
            self.assertAlmostEqual(result["remaining"], 100 / 7 - 22)

    def test_network_migration_preserves_today_and_ignores_reappearing_counters(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.STATE_FILE", Path(temp) / "traffic.json"):
            dashboard.STATE_FILE.write_text(json.dumps({"network": {
                "date": date.today().isoformat(), "total": [1000, 500], "last": [90000, 45000]}}))
            traffic = dashboard.DailyTraffic()
            self.assertEqual(traffic.update_network({"Wi-Fi renamed": [90000, 45000]}), [1000, 500])
            self.assertEqual(traffic.update_network({"Wi-Fi renamed": [90100, 45050]}), [1100, 550])
            self.assertEqual(traffic.update_network({}), [1100, 550])
            self.assertEqual(traffic.update_network({"Wi-Fi renamed": [95000, 47000]}), [1100, 550])
            self.assertEqual(traffic.update_network({"Wi-Fi renamed": [95010, 47005], "USB LAN": [900, 800]}), [1110, 555])
            self.assertEqual(traffic.update_network({"Wi-Fi renamed": [95010, 47005], "USB LAN": [1000, 850]}), [1210, 605])
            traffic.save()
            restored = dashboard.DailyTraffic()
            self.assertEqual(restored.update_network({"Wi-Fi renamed": [95020, 47010], "USB LAN": [1000, 850]}), [1220, 610])
            self.assertIsNone(restored.update_network(None))

    def test_network_adapter_reset_and_day_rollover(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.STATE_FILE", Path(temp) / "traffic.json"):
            traffic = dashboard.DailyTraffic()
            traffic.update_network({"LAN": [100, 50]})
            self.assertEqual(traffic.update_network({"LAN": [5, 2]}), [5, 2])
            traffic.state["network"]["date"] = "2000-01-01"
            self.assertEqual(traffic.update_network({"LAN": [900, 700]}), [0, 0])

    def test_physical_adapter_detection_uses_system_identity_and_cache(self):
        service = MagicMock()
        service.ExecQuery.return_value = [SimpleNamespace(Name="Renamed USB adapter")]
        with patch.dict(dashboard._PHYSICAL_NICS, names=None, ts=0), \
             patch("dashboard.time.monotonic", return_value=100), \
             patch("dashboard.pythoncom.CoInitialize"), patch("dashboard.pythoncom.CoUninitialize"), \
             patch("dashboard.win32com.client.GetObject", return_value=service) as get:
            self.assertEqual(dashboard.physical_interfaces(), {"Renamed USB adapter"})
            self.assertEqual(dashboard.physical_interfaces(), {"Renamed USB adapter"})
            get.assert_called_once()
            self.assertIn("HardwareInterface=True AND Virtual=False", service.ExecQuery.call_args.args[0])

    def test_rotation_preserves_descriptor_and_fault_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dash_diag.log"
            log = dashboard.DiagnosticLog(path, max_bytes=8)
            try:
                descriptor = log.fileno()
                log.write("old-data\n")
                log.write("new\n")
                self.assertEqual(log.fileno(), descriptor)
                self.assertEqual(path.with_name("dash_diag.1.log").read_text(), "old-data\n")
                log.flush()
                faulthandler.dump_traceback(file=log)
                self.assertIn("test_rotation_preserves_descriptor", path.read_text())
                self.assertTrue(path.read_text().startswith("new\n"))
            finally:
                log.close()

    def test_rotation_failure_keeps_original_and_threads_do_not_lose_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dash_diag.log"
            log = dashboard.DiagnosticLog(path, max_bytes=1)
            try:
                log.write("original\n")
                with patch("dashboard.shutil.copyfile", side_effect=OSError("backup failed")):
                    log.write("retained\n")
                log.flush()
                self.assertEqual(path.read_text(), "original\nretained\n")
                log.max_bytes = 1_000_000
                workers = [threading.Thread(target=log.write, args=(f"thread-{i}\n",)) for i in range(10)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()
                log.flush()
                self.assertEqual(len(path.read_text().splitlines()), 12)
            finally:
                log.close()

    def test_renderer_crash_recovery_is_bounded_and_recovers_after_quiet_period(self):
        web, tray, qt = MagicMock(), MagicMock(), MagicMock()
        failures = []
        callback = test_audit.AuditRegressionTest().main_callback("_render_gone", web=web, tray=tray,
            QtCore=qt, _render_failures=failures, _log_event=MagicMock())
        with patch("dashboard.time.monotonic", side_effect=[100, 101, 102, 103, 170]):
            for _ in range(4):
                callback(1, -1)
            self.assertEqual([call.args[0] for call in qt.QTimer.singleShot.call_args_list], [1000, 2000, 3000])
            tray.showMessage.assert_called_once()
            callback(1, -1)
            self.assertEqual(qt.QTimer.singleShot.call_args.args[0], 1000)

    def test_startup_migrates_legacy_task_and_checkbox_reports_both(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.STARTUP", Path(temp) / "panel.lnk"), \
             patch("dashboard.startup_tasks") as tasks, patch("dashboard.win32com.client.Dispatch") as dispatch:
            task = SimpleNamespace(Enabled=True)
            tasks.return_value = [task]
            bridge = dashboard.Bridge.__new__(dashboard.Bridge)
            self.assertTrue(bridge.startup_enabled())
            shortcut = dispatch.return_value.CreateShortcut.return_value
            shortcut.Save.side_effect = lambda: dashboard.STARTUP.write_bytes(b"watchdog shortcut")
            self.assertTrue(bridge.set_startup(True))
            self.assertFalse(task.Enabled)
            self.assertIn("panel_watchdog.py", shortcut.Arguments)
            self.assertTrue(bridge.set_startup(False))
            self.assertFalse(dashboard.STARTUP.exists())
            self.assertFalse(bridge.startup_enabled())

    def test_startup_failure_restores_old_shortcut(self):
        class Task:
            @property
            def Enabled(self):
                return True

            @Enabled.setter
            def Enabled(self, value):
                if not value:
                    raise OSError("access denied")

        with tempfile.TemporaryDirectory() as temp, patch("dashboard.STARTUP", Path(temp) / "panel.lnk"), \
             patch("dashboard.startup_tasks", return_value=[Task()]):
            dashboard.STARTUP.write_bytes(b"original")
            bridge = dashboard.Bridge.__new__(dashboard.Bridge)
            self.assertFalse(bridge.set_startup(False))
            self.assertEqual(dashboard.STARTUP.read_bytes(), b"original")

    def test_watchdog_stops_after_explicit_quit(self):
        with tempfile.TemporaryDirectory() as temp, patch("panel_watchdog.STOP_FILE", Path(temp) / "stop"), \
             patch("PyQt5.QtCore.QSharedMemory"), patch("PyQt5.QtCore.QCoreApplication"), \
             patch("panel_watchdog.dashboard_running", return_value=True), \
             patch("panel_watchdog.launch") as launch, \
             patch("panel_watchdog.time.sleep", side_effect=lambda _: panel_watchdog.STOP_FILE.touch()):
            panel_watchdog.main()
            launch.assert_not_called()

    def test_watchdog_quick_relaunch_waits_for_old_instance(self):
        with tempfile.TemporaryDirectory() as temp, patch("panel_watchdog.STOP_FILE", Path(temp) / "stop"), \
             patch("PyQt5.QtCore.QSharedMemory") as shared, patch("PyQt5.QtCore.QCoreApplication"), \
             patch("panel_watchdog.dashboard_running", return_value=False), \
             patch("panel_watchdog.launch", side_effect=lambda: panel_watchdog.STOP_FILE.touch()) as launch, \
             patch("panel_watchdog.time.sleep"):
            shared.return_value.create.side_effect = [False, False, True]
            panel_watchdog.STOP_FILE.touch()
            panel_watchdog.main()
            launch.assert_called_once()
            self.assertEqual(shared.return_value.create.call_count, 3)

    def test_watchdog_duplicate_launch_keeps_running_instance(self):
        with tempfile.TemporaryDirectory() as temp, patch("panel_watchdog.STOP_FILE", Path(temp) / "stop"), \
             patch("PyQt5.QtCore.QSharedMemory") as shared, patch("PyQt5.QtCore.QCoreApplication"), \
             patch("panel_watchdog.launch") as launch, patch("panel_watchdog.time.sleep") as sleep:
            shared.return_value.create.return_value = False
            panel_watchdog.main()
            launch.assert_not_called()
            sleep.assert_not_called()

    def test_startup_tasks_only_recognizes_this_project_python_entries(self):
        def task(path, argument):
            return SimpleNamespace(Definition=SimpleNamespace(Actions=[
                SimpleNamespace(Type=0, Path=path, Arguments=argument)]))

        own = task("C:\\Python\\pythonw.exe", f'"{panel_watchdog.ROOT / "dashboard.py"}"')
        other_project = task("C:\\Python\\pythonw.exe", '"C:\\other\\dashboard.py"')
        other_program = task("C:\\other.exe", own.Definition.Actions[0].Arguments)
        with patch("win32com.client.Dispatch") as dispatch:
            dispatch.return_value.GetFolder.return_value.GetTasks.return_value = [own, other_project, other_program]
            self.assertEqual(panel_watchdog.startup_tasks(), [own])

    def test_opening_clients_does_not_mark_unconfirmed_sessions_read(self):
        with patch("dashboard._focus_window", return_value=True), patch("dashboard.seen_mark") as mark, \
             patch("dashboard._open_url", return_value=False), patch.dict(dashboard._WB_LAST_CLICK, clear=True):
            for kind in ("zcode", "scnet", "workbuddy"):
                dashboard._open_session_job(kind, "sid", "title")
            mark.assert_not_called()
            self.assertNotIn("sid", dashboard._WB_LAST_CLICK)

    def test_explicit_mark_only_acknowledges_the_displayed_snapshot(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            collector = SimpleNamespace(lock=threading.Lock(), request_refresh=MagicMock(), data={"sessions": [
                {"source": "scnet", "id": "sid", "stamp": 1234, "status": "idle", "unread": True}]})
            bridge = dashboard.Bridge(None, collector, None, None)
            row = collector.data["sessions"][0]
            dashboard.seen_mark("reviewed:scnet", "sid", 1234)
            collector.data["sessions"] = dashboard.review_idle_sessions([row], [])
            self.assertTrue(bridge.mark_session_seen("scnet", "sid", 1234))
            self.assertEqual(dashboard.seen_load()["dismissed:scnet:sid"], 1234)
            self.assertEqual(dashboard.review_idle_sessions([row], []), [])
            self.assertFalse(bridge.mark_session_seen("scnet", "missing", 1234))
            newer = {**row, "stamp": 1235}
            collector.data["sessions"] = dashboard.review_idle_sessions([newer], collector.data["sessions"])
            self.assertEqual(collector.data["sessions"], [newer])
            self.assertFalse(bridge.mark_session_seen("scnet", "sid", 1234))
            self.assertTrue(bridge.mark_session_seen("scnet", "sid", 1235))
            self.assertEqual(dashboard.review_idle_sessions([newer], []), [])
            running = {**newer, "status": "executing", "unread": False}
            collector.data["sessions"] = dashboard.review_idle_sessions([running], [])
            self.assertFalse(bridge.mark_session_seen("scnet", "sid", 1235))
            self.assertEqual(dashboard.review_idle_sessions([newer], collector.data["sessions"]), [newer])

    def test_new_turn_rearms_review_even_when_source_reuses_stamp(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            dashboard.seen_mark("reviewed:workbuddy", "sid", 1234)
            dashboard.seen_mark("workbuddy", "sid", 1234)
            idle = {"source": "workbuddy", "id": "sid", "stamp": 1234, "status": "idle", "unread": False}
            [read] = dashboard.review_idle_sessions([idle], [])
            self.assertTrue(read["read"])
            active = {**idle, "status": "executing"}
            previous = dashboard.review_idle_sessions([active], [read])
            for _ in range(2):
                previous = dashboard.review_idle_sessions([idle], previous)
                self.assertEqual(len(previous), 1)
                self.assertTrue(previous[0]["unread"])
                self.assertFalse(previous[0].get("read", False))
            dashboard.seen_mark("workbuddy", "sid", 1235)
            [reopened] = dashboard.review_idle_sessions([idle], previous)
            self.assertTrue(reopened["read"])

    def test_codex_completion_waits_for_a_new_receipt(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            active = {"source": "codex", "id": "sid", "stamp": 1234, "status": "executing", "unread": False}
            idle = {**active, "stamp": 1235, "status": "idle", "receipt": 8}
            waiting = dashboard.review_idle_sessions([idle], [active])
            self.assertTrue(waiting[0]["unread"])
            self.assertEqual(dashboard.review_idle_sessions([idle], waiting), waiting)
            [read] = dashboard.review_idle_sessions([{**idle, "receipt": 9}], waiting)
            self.assertTrue(read["read"])

    def test_workbuddy_old_active_receipt_does_not_read_new_completion(self):
        now = int(time.time() * 1000)
        event = {"kind": "completed", "ts": now, "focused": False}
        with patch("dashboard.workbuddy_sidebar", return_value={}), \
             patch("dashboard.workbuddy_db_index", return_value={"s": {"status": "working", "updated": now}}), \
             patch("dashboard.workbuddy_events", return_value={"sessions": {"s": event}, "seen": {"s": now - 1000}}), \
             patch("dashboard.seen_load", return_value={}), patch("dashboard.seen_mark"):
            item = dashboard.workbuddy_sessions()[0]
            self.assertTrue(item["unread"])
            self.assertEqual(item["status"], "idle")
            event.update(kind="prompt", ts=now - 2 * 3600_000)
            with patch("dashboard.workbuddy_db_index", return_value={"s": {
                    "status": "working", "updated": now - 2 * 3600_000}}):
                self.assertEqual(dashboard.workbuddy_sessions()[0]["status"], "unknown")

    def test_scnet_terminal_markers_override_old_failure_and_modification_time(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            for reason, status in (("stop", "idle"), ("error", "error"), ("toolUse", "executing"), (None, "unknown")):
                path.write_text(json.dumps({"type": "message", "message": {
                    "role": "assistant", "stopReason": reason, "content": [{"type": "text", "text": "step"}]}}) + "\n")
                self.assertEqual(dashboard.scnet_progress(path), (status, "step"))
            path.write_text(json.dumps({"type": "message", "message": {
                "role": "assistant", "stopReason": "stop", "content": []}}) + "\n")
            self.assertEqual(dashboard.scnet_progress(path)[0], "idle")
            path.write_text(json.dumps({"type": "message", "message": {
                "role": "assistant", "stopReason": "toolUse",
                "content": [{"type": "toolCall", "name": "AskUserQuestion"}]}}) + "\n")
            self.assertEqual(dashboard.scnet_progress(path)[0], "interactive")
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "message", "message": {
                    "role": "toolResult", "content": []}}) + "\n")
            self.assertEqual(dashboard.scnet_progress(path)[0], "executing")

    def test_codex_interruption_survives_ten_minutes_and_can_be_acknowledged(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}), \
             patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"), patch("dashboard._ROLLOUT_STATE", {}):
            root = Path(temp) / ".codex"
            root.mkdir()
            path = root / "rollout.jsonl"
            path.write_text(json.dumps({"type": "event_msg", "payload": {
                "type": "turn_aborted", "turn_id": "t", "reason": "interrupted"}}) + "\n")
            old = time.time() - 7200
            os.utime(path, (old, old))
            with closing(sqlite3.connect(root / "state_5.sqlite")) as state, state:
                state.executescript("""CREATE TABLE threads(id,name,title,rollout_path,archived,thread_source,updated_at);
                    CREATE TABLE thread_read_receipts(thread_id,first_unread_turn,last_published_turn);""")
                state.execute("INSERT INTO threads VALUES ('s','name','name',?,0,'user',0)", (str(path),))
            with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as history, history:
                history.executescript("""CREATE TABLE thread_turns(thread_id,turn_id,status,rollout_ordinal);
                    INSERT INTO thread_turns VALUES ('s','t','interrupted',1);""")
            self.assertEqual(dashboard.unread_sessions()[0]["status"], "interrupted")
            dashboard.seen_mark("codex", "s", int(old * 1000))
            self.assertEqual(dashboard.unread_sessions(), [])
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t"}}) + "\n")
            self.assertEqual(dashboard.review_idle_sessions(dashboard.unread_sessions(), []), [])


if __name__ == "__main__":
    unittest.main()

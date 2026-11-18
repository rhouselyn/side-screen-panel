import ast
import ctypes
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dashboard
import panel_watchdog


class AuditRegressionTest(unittest.TestCase):
    def main_callback(self, name, **scope):
        # 隔离测试嵌套回调，避免启动真实置顶窗口或修改用户桌面。
        source = ast.parse((dashboard.ROOT / "dashboard.py").read_text(encoding="utf-8"))
        main = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        callback = next(node for node in main.body if isinstance(node, ast.FunctionDef) and node.name == name)
        namespace = dict(vars(dashboard), **scope)
        exec(compile(ast.Module(body=[callback], type_ignores=[]), "dashboard.py", "exec"), namespace)
        return namespace[name]

    def test_display_docking_remembers_device_and_waits_for_valid_geometry(self):
        main, screenpad = MagicMock(), MagicMock()
        main.name.return_value, screenpad.name.return_value = "main", "pad"
        rect = dashboard.QtCore.QRect
        main.geometry.return_value = rect(0, -1800, 2880, 1800)
        screenpad.geometry.return_value = rect(0, 0, 2880, 864)
        with patch("win32api.EnumDisplayDevices", side_effect=lambda name, *_: SimpleNamespace(
                DeviceID=f"monitor#{name.upper()}#instance")):
            self.assertIsNone(dashboard.panel_display([main], main, None))
            self.assertIs(dashboard.panel_display([main, screenpad], main, None), screenpad)
            selected = dashboard.display_identity(screenpad)
            self.assertIs(dashboard.panel_display([main, screenpad], screenpad, selected), screenpad)
            main.geometry.return_value.moveTop(0)
            self.assertIsNone(dashboard.panel_display([main, screenpad], main, selected))
            self.assertIsNone(dashboard.panel_display([main], main, selected))

    def test_show_panel_stays_hidden_when_secondary_is_missing(self):
        window, topmost = MagicMock(), MagicMock()
        state = {"full_expected": False}
        show = self.main_callback("_show_panel", window=window, state=state,
                                  place_window=MagicMock(), _force_topmost=topmost)
        show()
        window.show.assert_not_called()
        topmost.assert_not_called()
        state["full_expected"] = True
        show()
        window.show.assert_called_once()
        topmost.assert_called_once()

    def test_failed_screenshot_save_does_not_report_success(self):
        web, tray = MagicMock(), MagicMock()
        web.grab.return_value.save.return_value = False
        save = self.main_callback("_save_shot", web=web, tray=tray, icon=None, _diag=MagicMock())
        save()
        self.assertEqual(tray.showMessage.call_args.args[0], "截图失败")

    def test_seen_updates_are_atomic_and_never_go_backwards(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            dashboard.seen_mark("wb", "one", 2000)
            dashboard.seen_mark("wb", "one", 1000)
            self.assertEqual(dashboard.seen_load(), {"wb:one": 2000})
            with patch("dashboard.os.replace", side_effect=OSError("disk unavailable")):
                dashboard.seen_mark("wb", "two", 3000)
            self.assertEqual(dashboard.seen_load(), {"wb:one": 2000})

    def test_concurrent_seen_updates_preserve_every_session(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.SEEN_FILE", Path(temp) / "seen.json"):
            original_load = dashboard.seen_load

            def slow_load():
                result = original_load()
                time.sleep(.005)
                return result

            with patch("dashboard.seen_load", side_effect=slow_load), ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda i: dashboard.seen_mark("wb", str(i), 1), range(16)))
            self.assertEqual(len(original_load()), 16)

    def test_log_timestamps_preserve_milliseconds_and_timezone(self):
        expected = int(datetime(2026, 11, 13, 1, 2, 3, 987000, tzinfo=timezone.utc).timestamp() * 1000)
        for stamp in ("2026-11-13T01:02:03.987Z", "2026-11-13T09:02:03.987+08:00"):
            self.assertEqual(dashboard._log_ts_ms(json.dumps({"timestamp": stamp})), expected)
        self.assertEqual(dashboard._log_ts_ms('{"timestamp":"invalid"}'), 0)

    def test_rollout_new_turn_clears_old_activity_and_step(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard._ROLLOUT_STATE", {}):
            path = Path(temp) / "rollout.jsonl"
            records = [
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "old answer"}]}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "new"}},
            ]
            path.write_text("\n".join(map(json.dumps, records)) + "\n", encoding="utf-8")
            stat = path.stat()
            self.assertEqual(dashboard.rollout_progress(path, stat.st_size, stat.st_mtime_ns),
                             ("new", "executing", None))

    def test_cpu_queries_valid_namespace_and_acpi_without_name(self):
        def service(moniker):
            self.assertNotIn(r"root\root", moniker)
            if moniker.endswith(r"root\wmi"):
                return SimpleNamespace(ExecQuery=lambda _: [SimpleNamespace(CurrentTemperature=3200)])
            raise OSError("not installed")

        with patch.dict(dashboard._CPU_TEMP, value=None, ts=0), \
             patch("dashboard.win32com.client.GetObject", side_effect=service), \
             patch("dashboard.pythoncom.CoInitialize"), patch("dashboard.pythoncom.CoUninitialize"):
            self.assertEqual(dashboard.cpu_temperature(), 46.9)

    def test_proxy_routes_http_and_https_through_same_port(self):
        response = MagicMock()
        response.json.return_value = {"mixed-port": 7890, "tun": {"enable": False}}
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.CLASH_CONFIG", Path(temp) / "clash.yaml"), \
             patch("dashboard.requests.request", return_value=response), \
             patch("dashboard.winreg.OpenKey"), patch("dashboard.winreg.QueryValueEx", return_value=(1, 0)), \
             patch("dashboard.egress_lookup", return_value=("203.0.113.1", "test")) as lookup:
            dashboard.CLASH_CONFIG.write_text("external-controller: 127.0.0.1:9090", encoding="utf-8")
            self.assertEqual(dashboard.proxy_info()["ip"], "203.0.113.1")
            self.assertEqual(lookup.call_args.args[0], {
                "http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})

    def test_tun_switch_checks_actual_clash_state_and_rejects_remote_controller(self):
        response = MagicMock()
        response.json.return_value = {"tun": {"enable": True}}
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.CLASH_CONFIG", Path(temp) / "clash.yaml"), \
             patch.object(dashboard, "TUN_STATE", Path(temp) / "tun.json", create=True), \
             patch("dashboard.requests.request", return_value=response) as request:
            collector = dashboard.Collector()
            collector.request_refresh = MagicMock()
            bridge = dashboard.Bridge(None, collector, None, None)
            dashboard.CLASH_CONFIG.write_text("external-controller: 127.0.0.1:9090", encoding="utf-8")
            self.assertTrue(bridge.set_tun(True))
            self.assertEqual(request.call_args_list[0].kwargs["json"], {"tun": {"enable": True}})
            self.assertFalse(bridge.set_tun(False))
            self.assertIs(json.loads(dashboard.TUN_STATE.read_text(encoding="utf-8")), True)
            response.json.return_value = {"tun": {"enable": False}}
            self.assertTrue(bridge.set_tun(False))
            self.assertEqual(json.loads(dashboard.TUN_STATE.read_text(encoding="utf-8")), False)
            response.json.side_effect = [{"tun": {"enable": True}}, {"tun": {"enable": False}}]
            restarted = dashboard.Collector()
            restarted.restore_tun()
            self.assertEqual(request.call_args_list[-2].kwargs["json"], {"tun": {"enable": False}})
            request.reset_mock()
            restarted.restore_tun()
            request.assert_not_called()
            dashboard.CLASH_CONFIG.write_text("external-controller: example.com:9090", encoding="utf-8")
            self.assertFalse(bridge.set_tun(True))
            request.assert_not_called()
        self.assertEqual(collector.request_refresh.call_count, 4)

    def test_each_collector_keeps_refresh_requested_while_it_is_busy(self):
        with patch("dashboard.DailyTraffic"):
            collector = dashboard.Collector()
        busy, release, slow_ready, slow_refreshed, quotas_refreshed = [threading.Event() for _ in range(5)]

        def quota():
            if busy.is_set():
                quotas_refreshed.set()
                raise StopIteration
            busy.set()
            release.wait(2)
            return {"rateLimits": {}}

        def slow_again():
            if not slow_ready.is_set():
                slow_ready.set()
                return {}
            slow_refreshed.set()
            raise StopIteration

        def run(method):
            try:
                method()
            except StopIteration:
                pass

        with patch.dict("os.environ", {"DASH_SKIP_QUOTA": ""}), \
             patch("dashboard.codex_limits", side_effect=quota), \
             patch("dashboard.deepseek_balance", return_value={}), \
             patch("dashboard.proxy_info", side_effect=slow_again):
            worker = threading.Thread(target=run, args=(collector.quotas,), daemon=True)
            slow_worker = threading.Thread(target=run, args=(collector.slow,), daemon=True)
            worker.start()
            slow_worker.start()
            try:
                self.assertTrue(busy.wait(1))
                self.assertTrue(slow_ready.wait(1))
                collector.request_refresh()
                # slow 清掉自己的信号时，quotas 仍在请求中。
                self.assertTrue(slow_refreshed.wait(1))
                release.set()
                self.assertTrue(quotas_refreshed.wait(1))
            finally:
                release.set()
                collector.request_refresh()
                worker.join(2)
                slow_worker.join(2)

    def test_fast_source_failure_does_not_hide_other_sources_or_freeze_hardware(self):
        with ExitStack() as stack:
            stack.enter_context(patch("dashboard.DailyTraffic"))
            collector = dashboard.Collector()
            emitted = []
            collector.updated.connect(lambda value: emitted.append(json.loads(value)))
            for name in ("fast", "slow", "quotas"):
                event = MagicMock()
                event.wait.side_effect = StopIteration
                collector._refresh[name] = event
            stack.enter_context(patch("dashboard.psutil.net_io_counters", return_value={}))
            stack.enter_context(patch("dashboard.physical_interfaces", return_value=set()))
            stack.enter_context(patch("dashboard.psutil.cpu_percent", return_value=10))
            stack.enter_context(patch("dashboard.psutil.disk_usage", return_value=SimpleNamespace(free=1, total=2, percent=50)))
            stack.enter_context(patch("dashboard.psutil.virtual_memory", return_value=SimpleNamespace(used=1, total=2)))
            stack.enter_context(patch("dashboard.clash_counters", return_value=None))
            stack.enter_context(patch("dashboard.cpu_temperature", return_value=None))
            stack.enter_context(patch("dashboard.recycle_size", return_value=0))
            stack.enter_context(patch("dashboard.unread_sessions", side_effect=OSError("missing database")))
            stack.enter_context(patch("dashboard.zcode_sessions", return_value=[{
                "id": "z", "source": "zcode", "status": "executing", "stamp": 1}]))
            stack.enter_context(patch("dashboard.workbuddy_sessions", side_effect=ValueError("bad snapshot")))
            stack.enter_context(patch("dashboard.scnet_sessions", return_value=[]))
            stack.enter_context(patch("dashboard._diag_url"))
            stack.enter_context(patch.dict(dashboard._GPU_CACHE, value=[20, 60, 1, 2], ts=time.monotonic()))
            collector.daily.update.return_value = None
            collector.daily.update_network.return_value = None
            with self.assertRaises(StopIteration):
                collector.fast()
            self.assertEqual(emitted[0]["sessions"][0]["id"], "z")
            self.assertIsNone(emitted[0]["cpu_temp"], "GPU temperature must not be labelled CPU")

    def test_window_title_pointer_matches_win32_signature(self):
        # A typed callback exercises ctypes conversion without touching any desktop window.
        signature = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.wintypes.HWND,
            ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
            ctypes.wintypes.UINT, ctypes.wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))

        def send(hwnd, msg, length, address, flags, timeout, result):
            text = ctypes.create_unicode_buffer("DisplayChangeMask")
            ctypes.memmove(address, text, ctypes.sizeof(text))
            return 1

        with patch("dashboard.ctypes.windll") as windll:
            windll.user32.SendMessageTimeoutW = signature(send)
            self.assertEqual(dashboard._window_title(1), "DisplayChangeMask")

    def test_optional_config_wrong_shape_falls_back(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), \
             patch("dashboard.BRIGHTNESS_CONFIG", Path(temp) / "brightness.json"), \
             patch("dashboard.DEEPSEEK_CONFIG", Path(temp) / "deepseek.json"):
            bridge = dashboard.Bridge.__new__(dashboard.Bridge)
            for content in ('[1]', '{"value": null}', '"text"'):
                dashboard.BRIGHTNESS_CONFIG.write_text(content, encoding="utf-8")
                self.assertEqual(bridge.get_brightness(), 68)
            dashboard.DEEPSEEK_CONFIG.write_text('[1]', encoding="utf-8")
            self.assertEqual(bridge.get_deepseek_key(), "")
            self.assertEqual(dashboard.deepseek_balance(), {"state": "未配置 Key"})

    def test_watchdog_does_not_confuse_tests_with_the_dashboard(self):
        fake = SimpleNamespace(info={"cmdline": ["python", "test_dashboard.py"]})
        with patch("psutil.process_iter", return_value=[fake]):
            self.assertFalse(panel_watchdog.dashboard_running())

    def test_traffic_recovers_bad_state_and_counts_after_counter_reset(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.STATE_FILE", Path(temp) / "traffic.json"):
            for content in ('[1]', '{"network": null}', '{"network": {"total": [], "last": null}}'):
                dashboard.STATE_FILE.write_text(content, encoding="utf-8")
                daily = dashboard.DailyTraffic()
                self.assertEqual(daily.update("network", [100, 50]), [0, 0])
                self.assertEqual(daily.update("network", [150, 75]), [50, 25])
                self.assertEqual(daily.update("network", [10, 5]), [60, 30])
                with ThreadPoolExecutor(4) as pool:
                    list(pool.map(lambda _: daily.save(), range(8)))
                self.assertEqual(json.loads(dashboard.STATE_FILE.read_text())["network"]["total"], [60, 30])

    def test_clash_invalid_config_does_not_kill_collectors(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.CLASH_CONFIG", Path(temp) / "clash.yaml"):
            for content in ("", "[]", "external-controller: 123"):
                dashboard.CLASH_CONFIG.write_text(content, encoding="utf-8")
                self.assertIsNone(dashboard.clash_counters())
                self.assertEqual(dashboard.proxy_info(), {"state": "状态不可用"})

    def test_scnet_missing_log_path_is_not_the_working_directory(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"USERPROFILE": temp}):
            folder = Path(temp) / ".sclaw" / "agent"
            folder.mkdir(parents=True)
            (folder / "threads.json").write_text(json.dumps({"threads": {"s": {"index": {
                "title": "no log", "updatedAt": 0}}}}), encoding="utf-8")
            self.assertEqual(dashboard.scnet_sessions()[0]["status"], "unknown")

    def test_rollout_same_size_replacement_and_malformed_record(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard._ROLLOUT_STATE", {}):
            path = Path(temp) / "rollout.jsonl"
            prefix = '{"type":"event_msg","payload": [1]}\n'
            data = prefix + json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "old"}}) + "\n"
            path.write_text(data, encoding="utf-8")
            size = path.stat().st_size
            self.assertEqual(dashboard.rollout_progress(path, size, 1)[0], "old")
            path.write_text(data.replace('"old"', '"new"'), encoding="utf-8")
            self.assertEqual(path.stat().st_size, size)
            self.assertEqual(dashboard.rollout_progress(path, size, 2)[0], "new")

    def test_build_clears_dynamic_demo_count_and_rejects_missing_bridge(self):
        import runpy
        import shutil

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("build_dashboard.py", "dashboard-demo.html"):
                shutil.copyfile(dashboard.ROOT / name, root / name)
            demo = root / "dashboard-demo.html"
            source = demo.read_text(encoding="utf-8").replace("6 项未查看", "99 项未查看")
            demo.write_text(source, encoding="utf-8")
            runpy.run_path(str(root / "build_dashboard.py"))
            output = (root / "dashboard.html").read_text(encoding="utf-8")
            self.assertNotIn("99 项未查看", output)
            self.assertNotIn('<li class="task"', output)
            self.assertIn('src="dashboard.js"', output)
            demo.write_text(source.replace('<dialog aria-labelledby="result-title">', '<dialog>'), encoding="utf-8")
            with self.assertRaises(ValueError):
                runpy.run_path(str(root / "build_dashboard.py"))
            self.assertEqual((root / "dashboard.html").read_text(encoding="utf-8"), output)


if __name__ == "__main__":
    unittest.main()

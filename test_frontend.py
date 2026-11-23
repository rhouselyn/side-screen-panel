import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PyQt5 import QtCore, QtWidgets, QtWebEngineWidgets
import dashboard


class FrontendRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "win32":
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_render_recovers_missing_gpu_preserves_nodes_and_opens_latest_title(self):
        socket = MagicMock()
        socket.recv.return_value = '{"id":1,"result":{"result":{"value":true}}}'
        with patch("dashboard._codex_debug_target", return_value="ws://test"), \
             patch("websocket.create_connection", return_value=socket):
            dashboard._codex_click_thread("wanted", "一二三四五六七八九十乙")
        navigation = json.loads(socket.send.call_args.args[0])["params"]["expression"]
        root = Path(__file__).resolve().parent
        html = (root / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace('<script src="qrc:///qtwebchannel/qwebchannel.js"></script>', '')
        bridge = """const qt = {webChannelTransport: {}};
          window.alert = () => {};
          window.opened = [];
          window.marked = [];
          window.keepChanges = [];
          window.copies = [];
          function QWebChannel(transport, ready) {
            ready({objects: {bridge: {
              update: {connect() {}}, startup_enabled(done) {done(false)},
              tun_finished: {connect(done) {window.finishTun = done}},
              request_tun(enabled) {window.requestedTun = enabled},
              get_keep_read_sessions(done) {done(true)},
              set_keep_read_sessions(keep, done) {window.keepChanges.push(keep); done(true)},
              copy_screenshot() {window.copies.push([
                document.querySelector('.controls-menu').hidden,
                document.querySelector('.controls > button').getAttribute('aria-expanded')])},
              open_session(...args) {window.opened.push(args)},
              mark_session_seen(kind, id, stamp, done) {window.marked.push([kind, id, stamp]); done(true)}
            }}});
          }
        """
        html = html.replace('<script src="dashboard.js"></script>',
                            '<script>' + bridge + (root / "dashboard.js").read_text(encoding="utf-8") + '</script>')
        page = QtWebEngineWidgets.QWebEnginePage()
        loop = QtCore.QEventLoop()
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        loaded = []
        page.loadFinished.connect(lambda ok: (loaded.append(ok), loop.quit()))
        page.setHtml(html)
        timer.start(10000)
        loop.exec_()
        timer.stop()
        self.assertEqual(loaded, [True], "page did not load")
        results = []
        script = """(() => {
          const data = {cpu: 10, cpu_temp: null, gpu: [20, 60, 512, 8192],
            memory: [100, 200], disk: [100, 200, 50], recycle: 0,
            sessions: [{id: 'one', source: 'codex', name: '一二三四五六七八旧标题',
                        step: 'running', status: 'executing', unread: false}]};
          render(data);
          const row = document.querySelector('.task');
          const observer = new MutationObserver(() => {});
          observer.observe(document.querySelector('.panel'), {subtree: true, childList: true,
            characterData: true, attributes: true});
          render(data);
          const repeatedMutations = observer.takeRecords().length;
          observer.disconnect();
          data.gpu = null;
          data.sessions[0].name = '一二三四五六七八新标题';
          render(data);
          row.querySelector('.task-name').click();
          const result = {repeatedMutations, sameRow: row === document.querySelector('.task'),
            gpu: document.querySelector('.metric.gpu .value').firstChild.nodeValue,
            cpuTemp: document.querySelector('.metric.cpu .temp').firstChild.nodeValue,
            button: row.querySelector('.task-name').tagName,
            title: row.querySelector('.task-name').title, opened: window.opened,
            keyType: document.querySelector('.ds-key').type};
          data.codex = {rateLimits: {planType: 'pro', primary: {usedPercent: 66,
            resetsAt: 2000000000, windowDurationMins: 10080}}};
          data.daily_budget = {budget: 10, remaining: 4, resetsAt: 1999740800};
          data.sessions = [{id: 'two', source: 'scnet', name: 'test', status: 'unknown', unread: true, stamp: 1234}];
          render(data);
          result.daily = document.querySelector('.window .value').firstChild.nodeValue;
          result.dailyReset = document.querySelector('.window .meta').textContent ===
            '预算 10% · ' + dateText(data.daily_budget.resetsAt).replace('重置', '更新');
          result.readVisible = !document.querySelector('.task-read').hidden;
          document.querySelector('.task-read').click();
          result.marked = window.marked;
          data.daily_budget = {budget: 0, remaining: -2, resetsAt: 1999740800};
          render(data);
          result.overrun = document.querySelector('.window .value').firstChild.nodeValue;
          result.zeroFill = document.querySelector('.window .track span').style.getPropertyValue('--fill');
          data.codex = {rateLimits: {planType: 'plus', primary: {usedPercent: 0,
            resetsAt: 2000000000, windowDurationMins: 300}}};
          data.sessions = [{id: 'claude', source: 'claude', name: 'Example', status: 'idle', unread: true, stamp: 4321}];
          data.proxy = {state: 'ready', tun: true};
          render(data);
          result.plusLabel = document.querySelector('.window .label').textContent;
          result.plusRemaining = document.querySelector('.window .value').firstChild.nodeValue;
          result.claudeSource = document.querySelector('.task-source.claude').textContent;
          result.tun = !document.querySelector('.tun').hidden && document.querySelector('.tun input').checked;
          const tunInput = document.querySelector('.tun input');
          tunInput.focus();
          result.tunStyle = [getComputedStyle(tunInput).webkitAppearance,
            getComputedStyle(tunInput).outlineStyle, getComputedStyle(tunInput).backgroundImage];
          tunInput.click();
          result.tunPending = [tunInput.checked, tunInput.disabled, window.requestedTun,
            getComputedStyle(tunInput).backgroundImage];
          render(data);
          result.tunDuringRefresh = tunInput.checked;
          window.finishTun(false);
          result.tunRollback = [tunInput.checked, tunInput.disabled];
          tunInput.click();
          window.finishTun(true);
          result.tunSuccess = [tunInput.checked, tunInput.disabled];

          const idleRow = document.querySelector('.task');
          const check = idleRow.querySelector('.task-read');
          const state = idleRow.querySelector('.task-state');
          result.idle = [state.textContent, check.hidden, check.textContent, check.title,
            getComputedStyle(check).color];
          result.idleStateColor = getComputedStyle(state).color;
          data.sessions[0] = {...data.sessions[0], unread: false, read: true};
          render(data);
          result.read = [state.textContent, check.hidden,
            idleRow === document.querySelector('.task'), check.textContent, check.title,
            getComputedStyle(check).color];
          result.readStateColor = getComputedStyle(state).color;
          data.sessions[0] = {...data.sessions[0], status: 'interactive', read: false, unread: true, step: '是否继续？'};
          render(data);
          result.question = [state.textContent, idleRow.querySelector('.task-step').textContent,
            check.hidden, getComputedStyle(idleRow).animationName];
          data.sessions[0] = {...data.sessions[0], status: 'executing', read: false, stamp: 4322};
          render(data);
          result.newTurn = [state.textContent, check.hidden, idleRow === document.querySelector('.task')];
          result.runningAnimation = getComputedStyle(idleRow).animationName;
          data.sessions[0] = {...data.sessions[0], status: 'idle', unread: true};
          render(data);
          result.newCompletion = [state.textContent, check.hidden, check.textContent,
            getComputedStyle(check).color];
          check.click();
          data.sessions = [];
          render(data);
          result.dismissed = !document.querySelector('.task');

          const setting = document.querySelector('.read-behavior');
          result.keepDefault = setting.value;
          setting.value = 'hide';
          setting.dispatchEvent(new Event('change'));
          result.keepChanges = window.keepChanges;
          document.querySelector('.controls > button').click();
          document.querySelector('.copy-screenshot').click();
          result.copies = window.copies;

          const fixture = document.createElement('div');
          document.body.append(fixture);
          function navigate(rows) {
            fixture.textContent = '';
            window.navHit = null;
            for (const [id, title] of rows) {
              const row = document.createElement('div');
              row.setAttribute('data-app-action-sidebar-thread-id', 'local:' + id);
              row.title = row.textContent = title;
              row.onclick = () => {window.navHit = id};
              fixture.append(row);
            }
            const clicked = Function('return ' + __NAV_SCRIPT__)();
            return [clicked, window.navHit];
          }
          result.navigation = [
            navigate([['other', '一二三四五六七八九十甲']]),
            navigate([['a', '一二三四五六七八九十乙'], ['b', '一二三四五六七八九十乙']]),
            navigate([['unique', '一二三四五六七八九十乙']]),
            navigate([['wanted', 'other title'], ['b', '一二三四五六七八九十乙']])
          ];
          return JSON.stringify(result);
        })()""".replace("__NAV_SCRIPT__", json.dumps(navigation))
        page.runJavaScript(script, lambda result: (results.append(result), loop.quit()))
        timer.start(5000)
        loop.exec_()
        timer.stop()
        self.assertTrue(results and results[0], "render failed or timed out")
        result = json.loads(results[0])
        self.assertEqual(result["repeatedMutations"], 0)
        self.assertTrue(result["sameRow"])
        self.assertEqual(result["gpu"], "—")
        self.assertEqual(result["cpuTemp"], "—")
        self.assertEqual(result["button"], "BUTTON")
        self.assertEqual(result["title"], "一二三四五六七八新标题")
        self.assertEqual(result["opened"], [["codex", "one", result["title"]]])
        self.assertEqual(result["keyType"], "password")
        self.assertEqual(result["daily"], "4")
        self.assertTrue(result["dailyReset"])
        self.assertEqual(result["overrun"], "-2")
        self.assertEqual(result["zeroFill"], "0%")
        self.assertEqual((result["plusLabel"], result["plusRemaining"]), ("5 小时", "100"))
        self.assertEqual(result["claudeSource"], "Claude Code")
        self.assertTrue(result["tun"])
        self.assertEqual(result["tunStyle"][0:2], ["none", "none"])
        self.assertIn("radial-gradient", result["tunStyle"][2])
        self.assertEqual(result["tunPending"], [False, True, False, "none"])
        self.assertFalse(result["tunDuringRefresh"])
        self.assertEqual(result["tunRollback"], [True, False])
        self.assertEqual(result["tunSuccess"], [False, False])
        self.assertTrue(result["readVisible"])
        self.assertEqual(result["idleStateColor"], "rgb(244, 155, 204)")
        self.assertEqual(result["readStateColor"], "rgb(179, 188, 198)")
        self.assertEqual(result["question"], ["待回复", "是否继续？", True, "question-pulse"])
        self.assertEqual(result["runningAnimation"], "none")
        self.assertEqual(result["idle"], ["待命", False, "×", "移除待命条目", "rgb(244, 155, 204)"])
        self.assertEqual(result["read"], ["已读", False, True, "✓", "移除已读条目", "rgb(179, 188, 198)"])
        self.assertEqual(result["newTurn"], ["执行", True, True])
        self.assertEqual(result["newCompletion"], ["待命", False, "×", "rgb(244, 155, 204)"])
        self.assertEqual(result["marked"], [["scnet", "two", 1234], ["claude", "claude", 4322]])
        self.assertTrue(result["dismissed"])
        self.assertEqual(result["keepDefault"], "keep")
        self.assertEqual(result["keepChanges"], [False])
        self.assertEqual(result["copies"], [[True, "false"]])
        self.assertEqual(result["navigation"], [[False, None], [False, None], [True, "unique"], [True, "wanted"]])
        page.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_code
import dashboard
import panel_paths


class ClaudeCodeTest(unittest.TestCase):
    def test_transcript_tracks_tool_wait_completion_and_read(self):
        sid = "01234567-aaaa-bbbb-cccc-0123456789ab"
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temp}):
            project = Path(temp) / "projects" / "example"
            project.mkdir(parents=True)
            path = project / (sid + ".jsonl")

            def append(kind, content, stop=None, **metadata):
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": kind, "cwd": temp, **metadata,
                                             "message": {"content": content, "stop_reason": stop}}) + "\n")

            append("user", "Organize the test fixtures")
            append("custom-title", None, customTitle="Fixture cleanup")
            append("assistant", [{"type": "tool_use", "name": "Bash"}])
            with patch("claude_code._live_sessions", return_value={sid}):
                [item] = claude_code.sessions({})
                self.assertEqual((item["name"], item["path"], item["status"], item["step"]),
                                 ("Fixture cleanup", temp, "executing", "执行: Bash"))
                append("assistant", [{"type": "tool_use", "name": "AskUserQuestion"}])
                self.assertEqual(claude_code.sessions({})[0]["status"], "interactive")
                append("assistant", [{"type": "text", "text": "Finished the cleanup."}], "end_turn")
                [item] = claude_code.sessions({})
                self.assertEqual(item["status"], "idle")
                self.assertTrue(dashboard._session_visible(item))
                [read] = claude_code.sessions({"claude:" + sid: item["stamp"]})
                self.assertFalse(dashboard._session_visible(read))

            with patch("claude_code.shutil.which", return_value="claude.exe"), patch("claude_code.subprocess.Popen") as start:
                self.assertFalse(claude_code.resume_session("not-a-session", temp))
                self.assertTrue(claude_code.resume_session(sid, temp))
                start.assert_called_once_with(["claude.exe", "--resume", sid],
                                          cwd=temp, creationflags=subprocess.CREATE_NEW_CONSOLE)


class LocalProfileTest(unittest.TestCase):
    def test_upgrade_copies_state_without_replacing_local_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            root, profile = Path(temp) / "checkout", Path(temp) / "profile"
            root.mkdir()
            profile.mkdir()
            (root / "tun-state.json").write_text("true", encoding="utf-8")
            (root / "brightness.json").write_text('{"value":68}', encoding="utf-8")
            (profile / "tun-state.json").write_text("false", encoding="utf-8")
            with patch.object(panel_paths, "ROOT", root), patch.object(panel_paths, "DATA_DIR", profile), \
                 patch.dict(os.environ):
                os.environ.pop("PANEL_DATA_DIR", None)
                panel_paths.prepare_data()
            self.assertEqual((profile / "tun-state.json").read_text(encoding="utf-8"), "false")
            self.assertEqual((profile / "brightness.json").read_text(encoding="utf-8"), '{"value":68}')

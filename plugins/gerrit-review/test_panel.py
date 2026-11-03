import copy
import http.client
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import findings
from gerrit import Client, GerritError
import panel
import server
import test_gerrit as fixtures


class FindingsTests(unittest.TestCase):
    # Reuse the real Git fixture, including deleted and binary files.
    setUpClass = classmethod(fixtures.ReviewTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ReviewTests.tearDownClass.__func__)
    setUp = fixtures.ReviewTests.setUp
    tearDown = fixtures.ReviewTests.tearDown
    error = fixtures.ReviewTests.error

    def test_save_and_poll_are_private_and_fixed(self):
        finding = {"priority": 2, "title": "Check boundary", "path": "src/add.py", "line": 2,
                   "description": "A fixture finding, never published."}
        result = findings.save(self.client, "123", self.revision, "Reviewed fixture", [finding])
        with patch.object(self.client, "ssh", side_effect=AssertionError("Polling must be local")):
            self.assertEqual(findings.get(self.client, "123", self.revision), result)
            self.assertFalse(findings.get(self.client, "123", self.base)["ready"])
            self.assertFalse(findings.get(self.client, "124", self.revision)["ready"])
        self.assertEqual(self.client.posts, [])
        config = copy.deepcopy(fixtures.CONFIG)
        config["servers"]["test"]["ssh_user"] = "another-user"
        other = Client(config=config, state=self.client.state)
        self.assertFalse(findings.get(other, "123", self.revision)["ready"])
        saved = next((self.client.state / "findings").glob("*.json"))
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_invalid_findings_and_old_side_lines(self):
        row = {"priority": 2, "title": "title", "path": "src/add.py", "line": 999, "description": "desc"}
        self.error("INVALID_ARGUMENT", findings.save, self.client, "123", self.revision, "Summary", [row])
        row.update(path="../secret", line=1)
        self.error("INVALID_PATH", findings.save, self.client, "123", self.revision, "Summary", [row])
        row.update(path="removed.txt", side="PARENT")
        self.assertTrue(findings.save(self.client, "123", self.revision, "Reviewed old code", [row])["ready"])
        self.error("REVISION_NOT_FOUND", findings.save, self.client, "123", "f" * 40, "Summary", [])


class AppTests(unittest.TestCase):
    def test_entrypoints_and_resource_over_stdio(self):
        inputs = [{"jsonrpc": "2.0", "id": i, "method": method, "params": params}
                  for i, (method, params) in enumerate([
                      ("initialize", {}), ("resources/list", {}), ("resources/read", {"uri": panel.URI})])]
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("server.py"))],
                                input="".join(json.dumps(x) + "\n" for x in inputs), universal_newlines=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(x)["result"] for x in result.stdout.splitlines()]
        self.assertIn("resources", rows[0]["capabilities"])
        self.assertEqual(rows[1]["resources"][0]["uri"], panel.URI)
        content = rows[2]["contents"][0]
        self.assertEqual(content["mimeType"], panel.MIME)
        self.assertIn("ui/initialize", content["text"])
        self.assertNotIn("/*PANEL_CODE*/", content["text"])
        self.assertNotIn('rpc("ui/message"', content["text"])
        self.assertNotIn('navigator.clipboard', content["text"])
        self.assertIn('id="session-compose"', content["text"])
        meta = server.tools()[0]["_meta"]
        self.assertEqual(meta["ui"]["resourceUri"], panel.URI)
        self.assertEqual(meta["openai/ui"]["entrypoints"], [{"type": "thread"}, {"type": "global"}])
        for tool in server.tools()[1:4]:
            self.assertEqual(tool["_meta"]["ui"]["visibility"], ["app"])
        with patch.object(panel, "load_config", side_effect=GerritError("NOT_CONFIGURED", "Configure first")):
            self.assertFalse(server.call("gerrit_open_panel", {})["isError"])

    def test_bootstrap_cannot_break_out_of_script(self):
        text = panel.html({"initialChange": "</script><script>alert(1)</script>"})
        self.assertNotIn("</script><script>alert", text)
        self.assertIn("\\u003c/script>", text)

    def test_loopback_auth_origin_and_read_only_surface(self):
        calls = []
        def call_tool(name, arguments):
            calls.append((name, arguments))
            return {"structuredContent": {"ok": True}, "isError": False}
        service = panel.make_server(call_tool)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        def request(method, path="/", data=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", service.server_port, timeout=5)
            connection.request(method, path, data, headers or {})
            response = connection.getresponse()
            body = response.read().decode()
            status, info = response.status, dict(response.getheaders())
            connection.close()
            return status, body, info
        try:
            status, body, headers = request("GET")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
            token = json.loads(re.search(r"window.GERRIT_PANEL_BOOT = (.*?);</script>", body).group(1))["token"]
            payload = json.dumps({"name": "gerrit_get_change", "arguments": {"change": "123"}})
            auth = {"Content-Type": "application/json", "X-Gerrit-Token": token}
            self.assertEqual(request("POST", "/api/tool", payload)[0], 403)
            for bad in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}, {"X-Gerrit-Token": "wrong"}):
                self.assertEqual(request("POST", "/api/tool", payload, dict(auth, **bad))[0], 403)
            for name in ["gerrit_publish_review", "gerrit_prepare_review", "gerrit_save_findings", "arbitrary"]:
                self.assertEqual(request("POST", "/api/tool", json.dumps({"name": name}), auth)[0], 403)
            self.assertEqual(calls, [])
            self.assertEqual(request("POST", "/api/tool", payload, auth)[0], 200)
            self.assertEqual(calls, [("gerrit_get_change", {"change": "123"})])
            self.assertEqual(request("GET", "/config.json")[0], 404)
            self.assertEqual(request("POST", "/api/tool", "[]", auth)[0], 403)
        finally:
            service.shutdown(); service.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import gerrit
from gerrit import Client, GerritError
import server


CONFIG = {"default_server": "test", "servers": {"test": {
    "web_url": "https://review.example.com", "ssh_host": "review.example.com",
    "ssh_user": "tester", "ssh_port": 29418, "allow_publish": True}}}


class FakeClient(Client):
    def __init__(self, repo, state, record, enabled=True):
        config = copy.deepcopy(CONFIG)
        config["servers"]["test"]["allow_publish"] = enabled
        super().__init__(config=config, state=state)
        self.repo = repo
        self.record = record
        self.posts = []
        self.fail_post = False

    def ssh(self, command, body=None):
        if command[:2] == ["gerrit", "query"]:
            return (json.dumps(self.record) + '\n{"type":"stats","rowCount":1}\n').encode()
        if command == ["gerrit", "version"]:
            return b"gerrit version test\n"
        if command[:2] == ["gerrit", "review"]:
            self.posts.append((command, json.loads(body)))
            if self.fail_post:
                raise GerritError("TIMEOUT", "test timeout")
            return b""
        raise AssertionError(command)

    def cache(self, resolved):
        return self.repo


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixture.name)
        def git(*args):
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                                     "-c", "user.name=Test", "-c", "user.email=test@example.com"] + list(args),
                                    cwd=str(cls.root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode:
                raise AssertionError(result.stderr.decode())
            return result.stdout.decode().strip()
        git("init")
        (cls.root / "src").mkdir()
        (cls.root / "AGENTS.md").write_text("Check callers and tests.\n")
        (cls.root / "src/add.py").write_text("def add(x):\n    return x + 1\n")
        (cls.root / "removed.txt").write_text("old version\n")
        (cls.root / "literal[1].txt").write_text("before\n")
        git("add", ".")
        git("commit", "-m", "base")
        cls.base = git("rev-parse", "HEAD")
        (cls.root / "src/add.py").write_text("def add(x):\n    return x + 2\n")
        (cls.root / "removed.txt").unlink()
        (cls.root / "literal[1].txt").write_text("after\n")
        (cls.root / "binary.dat").write_bytes(b"\x00binary")
        git("add", ".")
        git("commit", "-m", "patch")
        cls.revision = git("rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        first = {"number": 1, "revision": self.base, "ref": "refs/changes/23/123/1", "files": []}
        current = {"number": 2, "revision": self.revision, "ref": "refs/changes/23/123/2", "files": [
            {"file": "/COMMIT_MSG"}, {"file": "src/add.py", "type": "MODIFIED"},
            {"file": "removed.txt", "type": "DELETED"}, {"file": "literal[1].txt", "type": "MODIFIED"},
            {"file": "binary.dat", "type": "ADDED"}],
            "comments": [{"file": "src/add.py", "line": 2, "message": "existing", "timestamp": 2}]}
        self.record = {"number": 123, "project": "test/project", "branch": "main", "status": "NEW",
                       "subject": "patch", "currentPatchSet": current, "patchSets": [first, current],
                       "comments": [{"message": "cover", "timestamp": 1}]}
        self.client = FakeClient(self.root / ".git", self.storage.name, self.record)

    def tearDown(self):
        self.storage.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(GerritError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def draft(self):
        return self.client.prepare("123", self.revision, "Check result", [
            {"path": "src/add.py", "line": 2, "message": "Test finding"}])

    def test_urls_and_server_scope(self):
        self.assertEqual(gerrit.parse_change("123", self.client.profile), (123, None))
        self.assertEqual(gerrit.parse_change("https://review.example.com/c/test/project/+/123/2/src/add.py", self.client.profile), (123, 2))
        for value in ["https://evil.example/c/x/+/123", "https://review.example.com@evil.example/c/x/+/123",
                      "http://review.example.com/c/x/+/123", "https://review.example.com/c/x/+/123?access_token=secret"]:
            self.error("WRONG_SERVER", gerrit.parse_change, value, self.client.profile)

    def test_paths_do_not_escape_or_accept_options_as_host(self):
        for value in ["../secret", "/etc/passwd", "a/../../b", "a//b", "a\nb", "a\\b"]:
            self.error("INVALID_PATH", gerrit.file_path, value)
        invalid = dict(self.client.profile, ssh_host="-oProxyCommand=bad")
        self.error("INVALID_CONFIG", gerrit.validate_profile, invalid)

    def test_patchset_pinning_and_unowned_revision(self):
        self.assertEqual(self.client.resolve("https://review.example.com/c/x/+/123/1")["revision"], self.base)
        self.error("REVISION_MISMATCH", self.client.resolve, "https://review.example.com/c/x/+/123/1", self.revision)
        self.error("REVISION_NOT_FOUND", self.client.resolve, "123", "f" * 40)
        self.error("INVALID_REVISION", self.client.resolve, "123", "current")

    def test_invalid_ref_is_not_fetched(self):
        self.record["currentPatchSet"]["ref"] = "refs/heads/main"
        self.error("INVALID_REF", self.client.resolve, "123", self.revision)

    def test_change_metadata_matches_pinned_git_commit(self):
        data = self.client.change("123")
        self.assertEqual(data["revision"], self.revision)
        self.assertEqual(data["baseRevision"], self.base)
        self.assertEqual(data["commitMessage"].strip(), "patch")
        self.assertFalse(any(row["path"] == "/COMMIT_MSG" for row in data["files"]))

    def test_diff_paging_and_literal_file_path(self):
        data = self.client.diff("123", self.revision, path="src/add.py", max_lines=2)
        self.assertEqual(data["nextLine"], 3)
        more = self.client.diff("123", self.revision, path="src/add.py", start_line=3)
        self.assertIn("+    return x + 2", more["diff"])
        literal = self.client.diff("123", self.revision, path="literal[1].txt")
        self.assertIn("+after", literal["diff"])

    def test_file_lines_parent_deleted_and_binary(self):
        current = self.client.content("123", self.revision, "src/add.py", start_line=2, max_lines=1)
        parent = self.client.content("123", self.revision, "src/add.py", side="PARENT")
        self.assertEqual(current["content"], "2:     return x + 2")
        self.assertIn("x + 1", parent["content"])
        self.assertIn("old version", self.client.content("123", self.revision, "removed.txt", side="PARENT")["content"])
        self.error("FILE_NOT_FOUND", self.client.content, "123", self.revision, "removed.txt")
        binary = self.client.content("123", self.revision, "binary.dat")
        self.assertTrue(binary["binary"])
        self.assertIsNone(binary["content"])

    def test_root_commit_has_empty_tree_base(self):
        data = self.client.change("https://review.example.com/c/x/+/123/1")
        self.assertEqual(data["baseRevision"], gerrit.EMPTY_TREE)
        self.assertIn("+def add(x)", self.client.diff("123", self.base, path="src/add.py")["diff"])

    def test_search_finds_call_sites_without_regex(self):
        result = self.client.search("123", self.revision, "return x + 2")
        self.assertEqual(result["totalMatches"], 1)
        self.assertIn("src/add.py:2:", result["matches"][0])
        self.assertEqual(self.client.search("123", self.revision, ".*")["totalMatches"], 0)

    def test_comments_include_cover_and_pinned_inline(self):
        data = self.client.comments("123", self.revision, limit=1)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["nextIndex"], 1)
        self.assertEqual(self.client.comments("123", self.revision, start_index=1)["comments"][0]["message"], "existing")

    def test_current_patch_summary_keeps_detailed_patch_comments(self):
        self.record["currentPatchSet"] = {key: value for key, value in self.record["currentPatchSet"].items() if key != "comments"}
        data = self.client.comments("123", self.revision)
        self.assertEqual(data["total"], 2)

    def test_prepare_is_local_and_preserves_exact_preview(self):
        draft = self.draft()
        self.assertFalse(draft["published"])
        self.assertEqual(self.client.posts, [])
        self.assertEqual(draft["review"]["comments"]["src/add.py"][0]["line"], 2)
        path = Path(self.storage.name) / "drafts" / (draft["draftId"] + ".json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_prepare_rejects_invalid_or_unrelated_lines(self):
        for comment, code in [({"path": "src/add.py", "line": 200, "message": "x"}, "INVALID_LINE"),
                              ({"path": "AGENTS.md", "line": 1, "message": "x"}, "INVALID_COMMENT"),
                              ({"path": "binary.dat", "line": 1, "message": "x"}, "INVALID_COMMENT")]:
            self.error(code, self.client.prepare, "123", self.revision, "", [comment])

    def test_publish_disabled_by_default(self):
        draft = self.draft()
        self.client.profile["allow_publish"] = False
        self.error("PUBLISH_DISABLED", self.client.publish, draft["draftId"], draft["digest"])
        self.assertEqual(self.client.posts, [])

    def test_publish_stale_closed_tampered_and_expired_guards(self):
        draft = self.draft()
        self.error("DRAFT_CHANGED", self.client.publish, draft["draftId"], "0" * 64)
        self.record["currentPatchSet"] = dict(self.record["currentPatchSet"], revision="e" * 40, number=3, ref="refs/changes/23/123/3")
        self.error("STALE_PATCHSET", self.client.publish, draft["draftId"], draft["digest"])
        self.record["currentPatchSet"] = self.record["patchSets"][1]
        self.record["status"] = "MERGED"
        self.error("CHANGE_CLOSED", self.client.publish, draft["draftId"], draft["digest"])
        self.record["status"] = "NEW"
        path = Path(self.storage.name) / "drafts" / (draft["draftId"] + ".json")
        doc = json.loads(path.read_text());doc["createdAt"] = 1;doc["digest"] = self.client.digest(doc)
        gerrit.atomic_json(path, doc)
        self.error("DRAFT_EXPIRED", self.client.publish, draft["draftId"], doc["digest"])
        self.assertEqual(self.client.posts, [])

    def test_publish_once_no_votes_or_notifications(self):
        draft = self.draft()
        self.assertTrue(self.client.publish(draft["draftId"], draft["digest"])["published"])
        self.assertTrue(self.client.publish(draft["draftId"], draft["digest"])["alreadyPublished"])
        self.assertEqual(len(self.client.posts), 1)
        command, payload = self.client.posts[0]
        self.assertEqual(command[-1], self.revision)
        self.assertEqual(payload["notify"], "NONE")
        self.assertEqual(payload["drafts"], "KEEP")
        self.assertNotIn("labels", payload)
        self.assertNotIn("--submit", command)

    def test_uncertain_publish_is_never_blindly_retried(self):
        draft = self.draft()
        self.client.fail_post = True
        self.error("PUBLISH_UNCERTAIN", self.client.publish, draft["draftId"], draft["digest"])
        self.error("PUBLISH_UNCERTAIN", self.client.publish, draft["draftId"], draft["digest"])
        self.assertEqual(len(self.client.posts), 1)

    def test_draft_is_bound_to_server_and_account(self):
        draft = self.draft()
        self.client.profile["ssh_user"] = "different-user"
        self.error("WRONG_SERVER", self.client.publish, draft["draftId"], draft["digest"])

    def test_ssh_command_quotes_data_and_does_not_use_local_shell(self):
        client = Client(config=CONFIG, state=self.storage.name)
        with patch("gerrit.execute", return_value=(0, b"ok")) as run:
            client.ssh(["gerrit", "query", "--", "subject:hello; touch /tmp/not-executed"])
        args = run.call_args[0][0]
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertEqual(args[-1], "gerrit query -- 'subject:hello; touch /tmp/not-executed'")


class ProtocolTests(unittest.TestCase):
    def test_mcp_initialization_discovery_and_unknown_tool(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "unknown"}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": [1]}]
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("server.py"))],
            input="".join(json.dumps(item) + "\n" for item in messages), universal_newlines=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[1]["result"]["tools"]), 11)
        self.assertTrue(rows[2]["result"]["isError"])
        self.assertEqual(rows[3]["error"]["code"], -32602)

    def test_undeclared_arguments_are_rejected(self):
        self.assertEqual(server.call("gerrit_publish_review", {"draft_id": "x", "digest": "y", "labels": {"Code-Review": 2}})["structuredContent"]["error"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()

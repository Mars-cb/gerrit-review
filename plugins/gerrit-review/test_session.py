from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gerrit import Client, GerritError, atomic_json, private_dir
import review_session as session
from test_gerrit import CONFIG


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError("Timed out waiting for test session")


class FakeEngine:
    def __init__(self, notify, call_tool):
        self.notify, self.call_tool, self.calls, self.writes = notify, call_tool, [], []

    def write(self, item):
        self.writes.append(item)

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "config/read":
            return {"config": {"mcp_servers": {"external": {"enabled": True}}}}
        if method == "thread/start":
            return {"thread": {"id": "fixture-thread", "ephemeral": True}, "model": "fixture-model"}
        if method == "turn/start":
            return {"turn": {"id": "fixture-turn", "status": "inProgress"}}
        return {}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = Client(config=CONFIG, state=self.temp.name)
        self.conversation = session.Conversation(private_dir(Path(self.temp.name) / "session"), self.client, FakeEngine)
        until(lambda: self.conversation.status == "idle")

    def tearDown(self):
        self.temp.cleanup()

    def test_ephemeral_thread_and_tools_are_scoped(self):
        start = next(params for method, params in self.conversation.engine.calls if method == "thread/start")
        self.assertTrue(start["ephemeral"])
        self.assertEqual(start["environments"], [])
        self.assertEqual(start["sandbox"], "read-only")
        self.assertFalse(start["config"]["mcp_servers"]["external"]["enabled"])
        names = {tool["name"] for tool in start["dynamicTools"]}
        self.assertEqual(names, session.READ_TOOLS)
        self.assertNotIn("gerrit_publish_review", names)
        self.assertNotIn("gerrit_send_review", names)

    def test_public_deltas_before_completion_and_no_raw_reasoning(self):
        c = self.conversation
        c.send("Review fixture", "a" * 32)
        until(lambda: c.status == "inProgress")
        c.notification("item/reasoning/textDelta", {"delta": "raw-hidden-content"})
        c.notification("item/agentMessage/delta", {"threadId": c.thread_id, "itemId": "message", "delta": "Checking callers"})
        data = c.snapshot()
        self.assertEqual(data["status"], "inProgress")
        self.assertEqual(data["messages"][-1]["text"], "Checking callers")
        self.assertNotIn("raw-hidden-content", json.dumps(data))
        self.assertFalse(c.snapshot(data["cursor"])["changed"])
        c.notification("turn/completed", {"turn": {"id": "fixture-turn", "status": "failed", "error": {"message": "fixture failure"}}})
        self.assertEqual(c.snapshot()["status"], "failed")
        self.assertEqual(c.snapshot()["messages"][-1]["text"], "fixture failure")

    def test_retry_is_deduplicated_and_active_turn_is_not_duplicated(self):
        c = self.conversation
        c.send("first", "a" * 32)
        self.assertTrue(c.send("first", "a" * 32)["reused"])
        with self.assertRaises(GerritError) as raised:
            c.send("another", "b" * 32)
        self.assertEqual(raised.exception.code, "SESSION_BUSY")
        until(lambda: c.status == "inProgress")
        c.interrupt()
        self.assertEqual(c.status, "interrupting")
        c.notification("turn/completed", {"turn": {"id": "fixture-turn", "status": "interrupted"}})
        c.send("follow up", "c" * 32)
        until(lambda: len([x for x in c.engine.calls if x[0] == "turn/start"]) == 2)
        self.assertEqual(len([x for x in c.engine.calls if x[0] == "thread/start"]), 1)

    def test_dynamic_reads_cannot_escape_target_or_publish(self):
        c = self.conversation
        c.target = {"change": "123", "revision": "a" * 40}
        with patch("server.call") as call:
            for tool, arguments in [
                ("gerrit_publish_review", {}),
                ("gerrit_get_diff", {"change": "124", "revision": "a" * 40}),
                ("gerrit_get_diff", {"change": "123", "revision": "b" * 40}),
                ("gerrit_get_diff", {"change": "123", "revision": "a" * 40, "server": "other"})]:
                result = c.tool_call({"threadId": c.thread_id, "callId": tool + json.dumps(arguments), "tool": tool, "arguments": arguments})
                self.assertFalse(result["success"])
            call.assert_not_called()
            call.return_value = {"isError": False, "structuredContent": {"diff": "fixture"}}
            self.assertTrue(c.tool_call({"threadId": c.thread_id, "callId": "ok", "tool": "gerrit_get_diff",
                                        "arguments": {"change": "123", "revision": "a" * 40}})["success"])
            self.assertEqual(call.call_args[0][1]["server"], "test")

    def test_failed_connection_does_not_spawn_a_second_worker(self):
        directory, endpoint = session.paths(self.client)
        atomic_json(directory / "worker.json", {"token": "fixture-token"})
        with patch.object(session, "exchange", side_effect=TimeoutError()), patch.object(session.subprocess, "Popen") as spawn:
            with self.assertRaises(GerritError):
                session.request(self.client)
            spawn.assert_not_called()

    def test_uncertain_start_cannot_be_resent_as_a_new_turn(self):
        c = self.conversation
        with patch.object(c.engine, "rpc", side_effect=GerritError("CODEX_TIMEOUT", "fixture timeout")):
            c.send("first", "d" * 32)
            until(lambda: c.status == "uncertain")
        with self.assertRaises(GerritError) as raised:
            c.send("retry", "e" * 32)
        self.assertEqual(raised.exception.code, "SESSION_BUSY")


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, threading, time
lock=threading.Lock()
def emit(data):
 with lock:
  print(json.dumps(data),flush=True)
def complete(turn):
 emit({'method':'item/agentMessage/delta','params':{'threadId':'fixture-thread','turnId':turn,'itemId':'m'+turn,'delta':'Checking fixture'}})
 deadline=time.monotonic()+8
 while time.monotonic()<deadline and not os.path.exists(os.environ['GERRIT_REVIEW_TEST_LOG']+'.continue'):time.sleep(.01)
 emit({'method':'item/agentMessage/delta','params':{'threadId':'fixture-thread','turnId':turn,'itemId':'m'+turn,'delta':' complete'}})
 emit({'method':'turn/completed','params':{'threadId':'fixture-thread','turn':{'id':turn,'status':'completed'}}})
counter=0
for line in sys.stdin:
 row=json.loads(line); method=row.get('method')
 if not method:continue
 with open(os.environ['GERRIT_REVIEW_TEST_LOG'],'a') as log:log.write(json.dumps(row)+'\n')
 if 'id' not in row:continue
 result={}
 if method=='config/read':result={'config':{}}
 if method=='thread/start':result={'thread':{'id':'fixture-thread','ephemeral':True},'model':'fixture-model'}
 if method=='turn/start':
  counter+=1; turn=str(counter); result={'turn':{'id':turn,'status':'inProgress'}}
 emit({'id':row['id'],'result':result})
 if method=='turn/start':
  emit({'method':'turn/started','params':{'threadId':'fixture-thread','turn':{'id':turn}}})
  threading.Thread(target=complete,args=(turn,),daemon=True).start()
'''


class SharedWorkerTests(unittest.TestCase):
    def test_reopen_across_processes_reuses_one_conversation_and_streams(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary, log = root / "codex", root / "rpc.log"
            binary.write_text(FAKE_CODEX); binary.chmod(0o700)
            config = root / "config.json"; atomic_json(config, CONFIG)
            environment = {"GERRIT_REVIEW_CONFIG": str(config), "GERRIT_REVIEW_STATE": str(root / "data"),
                           "GERRIT_REVIEW_CODEX": str(binary), "GERRIT_REVIEW_TEST_LOG": str(log)}
            with patch.dict(os.environ, environment):
                client = Client()
                try:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        opened = list(pool.map(lambda _: session.request(client), range(4)))
                    self.assertEqual(len({x["sessionId"] for x in opened}), 1)
                    data = until(lambda: (lambda x: x if x["status"] == "idle" else None)(session.request(client)))
                    self.assertEqual(data["threadId"], "fixture-thread")
                    directory, endpoint = session.paths(client)
                    self.assertEqual(endpoint.stat().st_mode & 0o777, 0o600)
                    self.assertEqual((directory / "transcript.json").stat().st_mode & 0o777, 0o600)
                    process = subprocess.run([sys.executable, str(Path(__file__).with_name("server.py")), "call", "gerrit_review_session"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=dict(os.environ), timeout=10)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    reopened = json.loads(process.stdout)["structuredContent"]
                    self.assertEqual(reopened["sessionId"], data["sessionId"])
                    session.request(client, "send", message="Review fixture", request_id="a" * 32)
                    session.request(client, "send", message="Review fixture", request_id="a" * 32)
                    partial = until(lambda: (lambda x: x if x["status"] == "inProgress" and any(m["role"] == "assistant" for m in x["messages"]) else None)(session.request(client)))
                    self.assertEqual(partial["messages"][-1]["text"], "Checking fixture")
                    Path(str(log) + ".continue").touch()
                    until(lambda: session.request(client)["status"] == "completed")
                    session.request(client, "send", message="Follow up", request_id="b" * 32)
                    until(lambda: session.request(client)["status"] == "completed")
                    rows = [json.loads(x) for x in log.read_text().splitlines()]
                    self.assertEqual(sum(x["method"] == "thread/start" for x in rows), 1)
                    self.assertEqual(sum(x["method"] == "turn/start" for x in rows), 2)
                    self.assertTrue(next(x["params"] for x in rows if x["method"] == "thread/start")["ephemeral"])
                finally:
                    session.request(client, "shutdown")
                    _, endpoint = session.paths(client)
                    until(lambda: not endpoint.exists())


if __name__ == "__main__":
    unittest.main()

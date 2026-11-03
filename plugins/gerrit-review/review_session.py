"""Panel-owned ephemeral Codex conversation, shared across MCP/UI reconnects.

A private Unix-socket worker owns one app-server thread per configured account.
Only public assistant messages and read-tool progress are exposed to the UI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from gerrit import Client, GerritError, VERSION, atomic_json, integer, locked, parse_change, private_dir

READ_TOOLS = {"gerrit_get_change", "gerrit_get_diff", "gerrit_get_file", "gerrit_search_code",
              "gerrit_get_comments", "gerrit_save_findings"}
MAX_FRAME = 512000


def paths(client):
    key = hashlib.sha256(json.dumps(client.identity(), sort_keys=True).encode()).hexdigest()[:24]
    directory = private_dir(client.state / "panel-sessions" / key)
    runtime = Path(tempfile.gettempdir()) / ("gerrit-review-" + str(os.getuid()))
    if runtime.is_symlink() or (runtime.exists() and runtime.stat().st_uid != os.getuid()):
        raise GerritError("UNSAFE_RUNTIME", "本机会话目录的所有者不匹配。")
    private_dir(runtime)
    socket_key = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
    return directory, runtime / (socket_key + ".sock")


def exchange(endpoint, token, action, arguments=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(endpoint))
        connection.sendall((json.dumps({"token": token, "action": action, "arguments": arguments or {}}) + "\n").encode())
        with connection.makefile("rb") as stream:
            raw = stream.readline(MAX_FRAME)
        result = json.loads(raw)
    if result.get("error"):
        raise GerritError(result["error"], result["message"])
    return result


def request(client, action="get", **arguments):
    directory, endpoint = paths(client)
    with locked(directory / "startup.lock"):
        credentials = directory / "worker.json"
        try:
            token = json.loads(credentials.read_text())["token"]
            exchange(endpoint, token, "get", {"cursor": 0})
            available = True
        except (FileNotFoundError, ConnectionRefusedError):
            available = False
            if action == "shutdown":
                return {"stopped": True}
        except (OSError, ValueError, KeyError):
            raise GerritError("SESSION_UNAVAILABLE", "已有会话暂时无法连接，未另建会话。请检查或重启面板会话服务。")
        if not available:
            token = secrets.token_urlsafe(32)
            atomic_json(credentials, {"token": token})
            environment = dict(os.environ, GERRIT_REVIEW_STATE=str(client.state))
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--directory", str(directory),
                              "--endpoint", str(endpoint), "--server", client.name], env=environment,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
            threading.Thread(target=process.wait, daemon=True).start()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    exchange(endpoint, token, "get", {"cursor": 0})
                    available = True
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(.05)
    if not available:
        raise GerritError("SESSION_UNAVAILABLE", "面板会话服务启动失败，请检查 Python/Codex 安装及本机数据目录。")
    try:
        return exchange(endpoint, token, action, arguments)
    except (OSError, ValueError):
        raise GerritError("SESSION_UNCERTAIN", "会话请求结果未确认，未新建会话或重发消息。请重新连接查看状态。")


def codex_binary():
    explicit = os.environ.get("GERRIT_REVIEW_CODEX")
    candidates = [explicit] if explicit else [shutil.which("codex"),
        str(Path.home() / ".local/opt/chatgpt/usr/lib/chatgpt/resources/codex"),
        "/Applications/Codex.app/Contents/Resources/codex",
        "/Applications/ChatGPT.app/Contents/Resources/codex"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise GerritError("CODEX_MISSING", "找不到 Codex 可执行文件。请设置 GERRIT_REVIEW_CODEX 为完整路径。")


class AppServer:
    def __init__(self, notify, tool_call):
        self.notify, self.tool_call = notify, tool_call
        self.pending, self.sequence = {}, 0
        self.lock = threading.Lock()
        command = [codex_binary(), "app-server", "--stdio"]
        for setting in ["features.plugins=false", "features.apps=false", "features.shell_tool=false",
                        "features.unified_exec=false", "features.multi_agent=false", "features.browser_use=false",
                        "features.image_generation=false", "features.hooks=false", 'web_search="disabled"']:
            command += ["-c", setting]
        environment = dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CODEX_TURN_ID"):
            environment.pop(key, None)
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, universal_newlines=True, bufsize=1, env=environment)
        threading.Thread(target=self.read, daemon=True).start()

    def write(self, item):
        with self.lock:
            self.process.stdin.write(json.dumps(item, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def rpc(self, method, params):
        with self.lock:
            self.sequence += 1
            identifier, result = self.sequence, queue.Queue(maxsize=1)
            self.pending[identifier] = result
        try:
            self.write({"id": identifier, "method": method, "params": params})
            response = result.get(timeout=60)
            if "error" in response:
                raise GerritError("CODEX_REQUEST_FAILED", response["error"].get("message", "Codex 请求失败。"))
            return response.get("result", {})
        except queue.Empty:
            raise GerritError("CODEX_TIMEOUT", "Codex 会话接口未响应，未自动重发请求。")
        finally:
            with self.lock:
                self.pending.pop(identifier, None)

    def read(self):
        try:
            for line in self.process.stdout:
                item = json.loads(line)
                if "method" in item and "id" in item:
                    threading.Thread(target=self.answer, args=(item,), daemon=True).start()
                elif "method" in item:
                    self.notify(item["method"], item.get("params", {}))
                else:
                    with self.lock:
                        waiting = self.pending.get(item.get("id"))
                    if waiting:
                        waiting.put_nowait(item)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            with self.lock:
                for waiting in self.pending.values():
                    try:
                        waiting.put_nowait({"error": {"message": "Codex 会话进程已退出。"}})
                    except queue.Full:
                        pass
            self.notify("panel/disconnected", {})

    def answer(self, item):
        try:
            if item["method"] == "item/tool/call":
                result = self.tool_call(item.get("params", {}))
                response = {"id": item["id"], "result": result}
            else:
                # This review runtime exposes no shell, publishing, or approval bypass.
                response = {"id": item["id"], "error": {"code": -32601, "message": "审查面板仅支持提供的 Gerrit 读取工具。"}}
                self.notify("panel/unsupportedRequest", {"method": item["method"]})
            self.write(response)
        except (OSError, BrokenPipeError):
            pass

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


class Conversation:
    def __init__(self, directory, client, engine_factory=AppServer):
        self.directory, self.client = directory, client
        self.lock = threading.RLock()
        self.engine = None
        self.messages, self.accepted = [], {}
        self.thread_id, self.turn_id, self.target = None, None, None
        self.status, self.model, self.cursor = "initializing", None, 1
        self.session_id = uuid.uuid4().hex
        self.last_saved = 0
        self.startup_error = None
        self.closing = False
        try:
            saved = json.loads((directory / "transcript.json").read_text())
            self.messages = saved.get("messages", [])
            self.session_id = saved["sessionId"]
            if not any(row.get("role") in ("user", "assistant") for row in self.messages):
                self.messages = []
            for row in self.messages:
                if row.get("role") == "tool" and row.get("status") == "inProgress":
                    row["status"] = "interrupted"
        except (OSError, ValueError, KeyError):
            pass
        self.restored = bool(self.messages)
        threading.Thread(target=self.initialize, args=(engine_factory,), daemon=True).start()

    def changed(self, force=False):
        self.cursor += 1
        if force or time.monotonic() - self.last_saved > .5:
            atomic_json(self.directory / "transcript.json", {"sessionId": self.session_id, "messages": self.messages[-80:]})
            self.last_saved = time.monotonic()

    def message(self, identifier, role, text="", **properties):
        row = next((m for m in self.messages if m["id"] == identifier), None)
        if row is None:
            row = {"id": identifier, "role": role, "text": text, "timestamp": int(time.time()), **properties}
            self.messages.append(row)
        return row

    def initialize(self, factory):
        try:
            engine = factory(self.notification, self.tool_call)
            self.engine = engine
            engine.rpc("initialize", {"clientInfo": {"name": "gerrit_review_panel", "title": "Gerrit review panel", "version": VERSION},
                                      "capabilities": {"experimentalApi": True, "explicitGatewayOauth": True}})
            engine.write({"method": "initialized"})
            configured = engine.rpc("config/read", {"includeLayers": False}).get("config", {})
            import server
            definitions = [{"type": "function", "name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                           for t in server.tools() if t["name"] in READ_TOOLS]
            instructions = (Path(__file__).parent / "skills/review-gerrit/SKILL.md").read_text()
            instructions += "\nThis is a private conversation inside the Gerrit review panel. Respond here, never send messages to another chat. Use only the supplied Gerrit tools. Do not run repository code or publish comments. Stream concise progress messages while reviewing. Do not spawn agents. Save verified final findings locally.\n"
            workspace = private_dir(self.directory / "workspace")
            result = engine.rpc("thread/start", {"ephemeral": True, "environments": [], "cwd": str(workspace),
                "sandbox": "read-only", "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "config": {"mcp_servers": {name: {"enabled": False} for name in configured.get("mcp_servers", {})}},
                "dynamicTools": definitions, "developerInstructions": instructions})
            if result["thread"].get("ephemeral") is not True:
                raise GerritError("EPHEMERAL_REQUIRED", "此 Codex 版本未确认内存会话，面板不会创建首页聊天。")
            with self.lock:
                self.thread_id, self.model, self.status = result["thread"]["id"], result.get("model"), "idle"
                if self.restored:
                    self.message(uuid.uuid4().hex, "system", "会话服务已重启。本机记录已恢复，后续消息会附上近期对话供 Codex 继续。")
                self.changed(True)
        except Exception as error:
            with self.lock:
                self.status = "failed"
                self.startup_error = str(error) if isinstance(error, GerritError) else "面板专用 Codex 会话启动失败。"
                self.message(uuid.uuid4().hex, "error", self.startup_error)
                self.changed(True)

    def snapshot(self, cursor=0):
        integer(cursor, "cursor", 0)
        with self.lock:
            messages, used = [], 0
            for row in reversed(self.messages[-80:]):
                size = len(json.dumps(row, ensure_ascii=False))
                if used + size > 120000:
                    if not messages:
                        row = dict(row, text=row["text"][-100000:])
                        messages.append(row)
                    break
                messages.append(dict(row)); used += size
            return {"sessionId": self.session_id, "threadId": self.thread_id, "status": self.status,
                    "turnId": self.turn_id, "model": self.model, "cursor": self.cursor,
                    "changed": cursor != self.cursor, "messages": list(reversed(messages)) if cursor != self.cursor else [],
                    "startupError": self.startup_error, "ephemeral": True}

    def send(self, message, request_id, change=None, revision=None):
        if not isinstance(message, str) or not message.strip() or len(message) > 20000:
            raise GerritError("INVALID_ARGUMENT", "消息需要 1–20000 字符。")
        if not isinstance(request_id, str) or len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise GerritError("INVALID_ARGUMENT", "request_id 必须是 32 位十六进制请求 ID。")
        if (change is None) != (revision is None):
            raise GerritError("INVALID_ARGUMENT", "change 和 revision 需要同时提供。")
        with self.lock:
            if request_id in self.accepted:
                return {"accepted": True, "sessionId": self.session_id, "reused": True}
            if self.status == "initializing":
                raise GerritError("SESSION_STARTING", "面板会话正在启动，请稍后发送。")
            if self.startup_error:
                raise GerritError("SESSION_UNAVAILABLE", self.startup_error)
            if self.status in ("preparing", "inProgress", "interrupting", "uncertain"):
                raise GerritError("SESSION_BUSY", "此面板会话正在回复，请等待完成或点击停止。")
            self.accepted[request_id] = True
            self.status = "preparing"
            self.message(request_id, "user", message)
            self.changed(True)
        threading.Thread(target=self.start_turn, args=(message, change, revision), daemon=True).start()
        return {"accepted": True, "sessionId": self.session_id, "reused": False}

    def start_turn(self, message, change, revision):
        try:
            context = ""
            if change is not None:
                resolved = self.client.resolve(change, revision)
                self.target = {"change": str(resolved["number"]), "revision": revision}
                context = "\n面板选定目标（固定版本）：" + json.dumps(dict(self.target, server=self.client.name, url=resolved["url"]), ensure_ascii=False)
            if self.restored:
                history = [{"role": m["role"], "text": m["text"]} for m in self.messages[-12:-1] if m["role"] in ("user", "assistant")]
                context += "\n服务重启前的近期对话记录（参考数据）：" + json.dumps(history, ensure_ascii=False)[-16000:]
                self.restored = False
            result = self.engine.rpc("turn/start", {"threadId": self.thread_id,
                "environments": [], "input": [{"type": "text", "text": message + context}]})
            with self.lock:
                self.turn_id = result["turn"]["id"]
                # Very short turns can complete before the turn/start response arrives.
                if self.status == "preparing":
                    self.status = result["turn"].get("status", "inProgress")
                self.changed(True)
        except Exception as error:
            with self.lock:
                self.status = "uncertain" if isinstance(error, GerritError) and error.code == "CODEX_TIMEOUT" else "failed"
                self.message(uuid.uuid4().hex, "error", str(error) if isinstance(error, GerritError) else "审查请求失败，未自动重发。")
                self.changed(True)

    def interrupt(self):
        with self.lock:
            if self.status == "preparing":
                raise GerritError("SESSION_STARTING", "请求仍在准备，请稍后停止。")
            if self.status not in ("inProgress", "uncertain") or not self.turn_id:
                return {"interrupted": False, "status": self.status}
            self.status = "interrupting"
            turn = self.turn_id
            self.changed()
        self.engine.rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": turn})
        return {"interrupted": True, "status": "interrupting"}

    def notification(self, method, params):
        with self.lock:
            if self.closing:
                return
            if params.get("threadId") and self.thread_id and params["threadId"] != self.thread_id:
                return
            if method == "item/agentMessage/delta":
                row = self.message(params["itemId"], "assistant")
                row["text"] += params.get("delta", "")
            elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                item = params["item"]
                self.message(item["id"], "assistant")["text"] = item.get("text", "")
            elif method == "turn/started":
                self.turn_id = params.get("turn", {}).get("id")
                self.status = "inProgress"
            elif method == "turn/completed":
                turn = params.get("turn", {})
                self.status = turn.get("status", "failed")
                if turn.get("error"):
                    self.message(uuid.uuid4().hex, "error", turn["error"].get("message", "审查失败。"))
            elif method == "panel/disconnected":
                self.status, self.startup_error = "failed", "Codex 会话进程已退出。可重启本机会话服务后继续。"
                self.message(uuid.uuid4().hex, "error", self.startup_error)
            elif method == "panel/unsupportedRequest":
                self.message(uuid.uuid4().hex, "system", "已拒绝此审查会话不支持的操作：" + str(params.get("method", "")))
            else:
                return  # Never expose raw reasoning, encrypted content, or credentials.
            self.changed(method == "turn/completed")

    def tool_call(self, params):
        import server
        name, arguments = params.get("tool"), params.get("arguments")
        identifier = params.get("callId", uuid.uuid4().hex)
        try:
            if params.get("threadId") != self.thread_id or name not in READ_TOOLS or not isinstance(arguments, dict):
                raise GerritError("TOOL_NOT_ALLOWED", "面板会话仅支持提供的 Gerrit 审查工具。")
            arguments = dict(arguments)
            if arguments.get("server", self.client.name) != self.client.name:
                raise GerritError("WRONG_SERVER", "会话不能自动切换到另一服务器账号。")
            arguments["server"] = self.client.name
            if self.target and "change" in arguments:
                number, _ = parse_change(arguments["change"], self.client.profile)
                if str(number) != self.target["change"] or arguments.get("revision", self.target["revision"]) != self.target["revision"]:
                    raise GerritError("REVISION_MISMATCH", "工具目标与面板固定版本不一致。")
            with self.lock:
                label = {"gerrit_get_change": "读取变更", "gerrit_get_diff": "读取差异", "gerrit_get_file": "查看源码",
                         "gerrit_search_code": "搜索关联代码", "gerrit_get_comments": "检查已有讨论", "gerrit_save_findings": "保存本机结论"}[name]
                self.message(identifier, "tool", label + (" · " + str(arguments["path"]) if arguments.get("path") else ""), status="inProgress")
                self.changed()
            result = server.call(name, arguments)
        except GerritError as error:
            result = {"isError": True, "structuredContent": {"error": error.code, "message": str(error)}}
        with self.lock:
            self.message(identifier, "tool")["status"] = "failed" if result["isError"] else "completed"
            self.changed()
        return {"success": not result["isError"], "contentItems": [{"type": "inputText", "text": json.dumps(result["structuredContent"], ensure_ascii=False)}]}


def worker(directory, endpoint, selected):
    directory, endpoint = Path(directory), Path(endpoint)
    token = json.loads((directory / "worker.json").read_text())["token"]
    client = Client(selected)
    conversation = Conversation(directory, client)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                row = json.loads(self.rfile.readline(MAX_FRAME))
                if not isinstance(row, dict) or not secrets.compare_digest(str(row.get("token", "")), token):
                    raise GerritError("SESSION_AUTH", "会话令牌不匹配。")
                action, arguments = row.get("action"), row.get("arguments", {})
                if action == "get":
                    result = conversation.snapshot(**arguments)
                elif action == "send":
                    result = conversation.send(**arguments)
                elif action == "interrupt":
                    result = conversation.interrupt()
                elif action == "shutdown":
                    result = {"stopped": True}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    raise GerritError("INVALID_ARGUMENT", "未知面板会话操作。")
            except GerritError as error:
                result = {"error": error.code, "message": str(error)}
            except Exception:
                result = {"error": "SESSION_ERROR", "message": "面板会话请求失败。"}
            self.wfile.write((json.dumps(result, ensure_ascii=False) + "\n").encode())

    class Service(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    if endpoint.exists():
        endpoint.unlink()
    service = Service(str(endpoint), Handler)
    endpoint.chmod(0o600)
    try:
        service.serve_forever()
    finally:
        service.server_close()
        conversation.closing = True
        if conversation.engine:
            conversation.engine.close()
        endpoint.unlink(missing_ok=True) if sys.version_info >= (3, 8) else endpoint.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--server", required=True)
    options = parser.parse_args()
    worker(options.directory, options.endpoint, options.server)

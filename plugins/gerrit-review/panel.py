"""MCP App resource and an optional loopback-only browser transport."""
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets

from gerrit import GerritError, VERSION, load_config, validate_profile

URI = "ui://gerrit-review/panel-v" + VERSION + ".html"
MIME = "text/html;profile=mcp-app"
ROOT = Path(__file__).parent / "web"
# The browser bridge intentionally exposes no Gerrit publishing endpoint.
HTTP_TOOLS = {"gerrit_open_panel", "gerrit_connection_status", "gerrit_get_change", "gerrit_get_diff",
              "gerrit_get_file", "gerrit_search_code", "gerrit_get_comments", "gerrit_get_findings",
              "gerrit_review_session", "gerrit_send_review", "gerrit_interrupt_review"}


def bootstrap(change=None, server=None):
    data = {"version": VERSION, "servers": [], "defaultServer": server, "initialChange": change or ""}
    try:
        config = load_config()
        for name, raw in config["servers"].items():
            profile = validate_profile(raw)
            data["servers"].append({"name": name, "webUrl": profile["web_url"]})
        data["defaultServer"] = server or config.get("default_server") or data["servers"][0]["name"]
    except GerritError as error:
        data["configurationError"] = str(error)
    return data


def html(boot=None):
    settings = json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")
    return (ROOT / "panel.html").read_text().replace("/*PANEL_BOOT*/null", settings).replace(
        "/*PANEL_CODE*/", (ROOT / "panel.js").read_text())


def resource():
    return {"uri": URI, "mimeType": MIME, "text": html(), "_meta": {
        "ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}},
        "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"], "preferredDisplayMode": "fullscreen"}}}


def make_server(call_tool, port=0, change=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Source and request URLs never enter access logs.

        def reply(self, status, body, kind="application/json; charset=utf-8"):
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def trusted(self):
            expected = "127.0.0.1:{}".format(self.server.server_port)
            return self.headers.get("Host") == expected and self.headers.get("Origin", "http://" + expected) == "http://" + expected

        def do_GET(self):
            if not self.trusted():
                return self.reply(403, '{}')
            if self.path != "/":
                return self.reply(404, '{}')
            boot = bootstrap(change)
            boot.update({"transport": "http", "token": token})
            self.reply(200, html(boot), "text/html; charset=utf-8")

        def do_POST(self):
            if not self.trusted() or not hmac.compare_digest(self.headers.get("X-Gerrit-Token", "").encode("utf-8"), token.encode("ascii")):
                return self.reply(403, '{}')
            if self.path != "/api/tool":
                return self.reply(404, '{}')
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get_content_type() != "application/json":
                    return self.reply(400, '{}')
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or request.get("name") not in HTTP_TOOLS:
                    return self.reply(403, '{}')
                result = call_tool(request["name"], request.get("arguments", {}))
            except (ValueError, TypeError):
                return self.reply(400, '{}')
            self.reply(200, json.dumps(result, ensure_ascii=False))

    service = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    service.daemon_threads = True
    return service


def run(call_tool, port=0, change=None):
    service = make_server(call_tool, port, change)
    print("Gerrit panel: http://127.0.0.1:{}/".format(service.server_port), flush=True)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.server_close()

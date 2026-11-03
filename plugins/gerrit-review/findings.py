"""Private review results shared by the model tools and panel. No Gerrit writes."""
import hashlib
import json
import time

from gerrit import GerritError, SHA, atomic_json, file_path, integer, parse_change


def location(client, change, revision):
    number, _ = parse_change(change, client.profile)
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise GerritError("INVALID_REVISION", "需要完整的 40 位提交 SHA。")
    key = hashlib.sha256(json.dumps([client.identity(), number, revision], sort_keys=True).encode()).hexdigest()
    return number, client.state / "findings" / (key + ".json")


def get(client, change, revision):
    number, path = location(client, change, revision)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"ready": False, "change": number, "revision": revision, "findings": []}


def save(client, change, revision, summary, findings):
    number, path = location(client, change, revision)
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 20000:
        raise GerritError("INVALID_ARGUMENT", "summary 需要 1–20000 字符的审查结论。")
    if not isinstance(findings, list) or len(findings) > 50:
        raise GerritError("INVALID_ARGUMENT", "findings 需要最多 50 项的数组。")
    resolved = client.resolve(change, revision)
    repo = client.cache(resolved)
    checked, lengths = [], {}
    for item in findings:
        required = {"priority", "title", "path", "line", "description"}
        if not isinstance(item, dict) or set(item) - (required | {"side"}) or required - set(item):
            raise GerritError("INVALID_ARGUMENT", "发现项字段不完整或包含未知字段。")
        row = dict(item)
        integer(row["priority"], "priority", 0, 3)
        integer(row["line"], "line")
        file_path(row["path"])
        row.setdefault("side", "REVISION")
        if row["side"] not in ("REVISION", "PARENT"):
            raise GerritError("INVALID_ARGUMENT", "side 必须为 REVISION 或 PARENT。")
        for name, maximum in (("title", 300), ("description", 12000)):
            if not isinstance(row[name], str) or not row[name].strip() or len(row[name]) > maximum:
                raise GerritError("INVALID_ARGUMENT", "发现项标题或说明为空或过长。")
        key = (row["path"], row["side"])
        if key not in lengths:
            commit = revision if row["side"] == "REVISION" else client.parent(repo, revision, 1)
            body = client.blob(repo, commit, row["path"])
            if b"\x00" in body:
                raise GerritError("INVALID_ARGUMENT", "二进制文件不支持源码行定位。")
            try:
                lengths[key] = len(body.decode("utf-8").splitlines())
            except UnicodeError:
                raise GerritError("UNSUPPORTED_ENCODING", "文件不是 UTF-8 文本。")
        if row["line"] > lengths[key]:
            raise GerritError("INVALID_ARGUMENT", "发现项行号超出所选版本的文件。")
        checked.append(row)
    result = {"ready": True, "server": client.name, "change": number, "revision": revision,
              "patchset": int(resolved["patch"]["number"]), "summary": summary,
              "findings": checked, "completedAt": int(time.time()), "published": False}
    if len(json.dumps(result, ensure_ascii=False)) > 150000:
        raise GerritError("RESPONSE_TOO_LARGE", "审查结果过大，请精简说明。")
    atomic_json(path, result)
    return result

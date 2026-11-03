#!/usr/bin/env python3
"""Validate and package public source without including any runtime data."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins/gerrit-review"
PLUGIN_FILES = (
    "plugin.json", "mcp.json", "README.md", "LICENSE", "config.example.json",
    "gerrit.py", "server.py", "findings.py", "panel.py", "test_gerrit.py", "test_panel.py", "assets/icon.svg",
    "web/panel.html", "web/panel.js", "web/test_panel.cjs",
    "skills/review-gerrit/SKILL.md",
)
ROOT_FILES = (
    "README.md", "LICENSE", "PRIVACY.md", ".gitignore",
    ".agents/plugins/marketplace.json", "docs/RELEASING.md", "scripts/package_release.py",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(tag=None):
    manifest = json.loads((PLUGIN / "plugin.json").read_text())
    require(manifest.get("$schema") == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "Invalid plugin schema")
    require(manifest.get("name") == "gerrit-review", "Unexpected plugin identity")
    version = manifest["version"]
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version), "Invalid plugin version")
    expected = "v" + version
    require(not tag or tag == expected, "Release tag must match plugin version " + expected)
    constants = {}
    for node in ast.parse((PLUGIN / "gerrit.py").read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "VERSION":
                    constants[target.id] = ast.literal_eval(node.value)
    require(constants.get("VERSION") == version, "gerrit.py VERSION differs from plugin.json")
    market = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    require(market["plugins"][0]["name"] == manifest["name"], "Marketplace identity mismatch")
    require(market["plugins"][0]["source"]["path"] == "./plugins/gerrit-review", "Unexpected marketplace source")
    connection = json.loads((PLUGIN / "mcp.json").read_text())
    require(connection.get("$schema") == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json", "Invalid MCP schema")
    require(connection["mcpServers"]["gerrit"]["args"] == ["${PLUGIN_ROOT}/server.py"], "Unexpected server entrypoint")
    interface = manifest["extensions"]["com.openai"]["interface"]
    require(interface["category"] == "Developer Tools", "Unexpected category")
    require(0 < len(interface["shortDescription"]) <= 30, "Invalid listing subtitle")
    for key in ("logo", "composerIcon"):
        asset = PLUGIN / interface[key]
        asset.resolve().relative_to(PLUGIN.resolve())
        require(asset.is_file() and not asset.is_symlink(), "Missing or linked icon")
    for parent, names in ((PLUGIN, PLUGIN_FILES), (ROOT, ROOT_FILES)):
        for name in names:
            source = parent / name
            require(source.is_file() and not source.is_symlink(), "Missing or linked file: " + name)
    skill = (PLUGIN / "skills/review-gerrit/SKILL.md").read_text()
    require(skill.startswith("---\n"), "Missing skill frontmatter")
    header = dict(line.split(":", 1) for line in skill.split("---", 2)[1].strip().splitlines())
    require(header.get("name", "").strip() == "review-gerrit", "Skill identity mismatch")
    require(bool(header.get("description", "").strip()), "Missing skill description")
    example = json.loads((PLUGIN / "config.example.json").read_text())
    require(all(profile.get("allow_publish") is False for profile in example["servers"].values()), "Example must disable publishing")
    return expected


def archive(path, entries):
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as bundle:
        for source, target in sorted(entries, key=lambda item: item[1]):
            info = zipfile.ZipInfo(target, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, source.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    tag = validate(args.tag or None)
    args.output.mkdir(parents=True, exist_ok=True)
    plugin_zip = args.output / ("gerrit-review-" + tag + ".zip")
    archive(plugin_zip, [(PLUGIN / name, name) for name in PLUGIN_FILES])
    market_zip = args.output / ("gerrit-review-marketplace-" + tag + ".zip")
    prefix = "gerrit-review-" + tag + "/"
    entries = [(ROOT / name, prefix + name) for name in ROOT_FILES]
    entries.extend((PLUGIN / name, prefix + "plugins/gerrit-review/" + name) for name in PLUGIN_FILES)
    archive(market_zip, entries)
    hashes = []
    for path in (plugin_zip, market_zip):
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)
        print(path)
    (args.output / "SHA256SUMS").write_text("\n".join(hashes) + "\n")
    if args.github_output:
        with args.github_output.open("a") as stream:
            stream.write("tag=" + tag + "\n")


if __name__ == "__main__":
    main()

"""The Locus plugin in plugin/ is consistent with the package source.

After changing src/ or plugin/requirements.in, run tools/build_plugin.py.
"""

import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

import langgraph_workflow

ROOT = Path(__file__).parents[2]
PLUGIN = ROOT / "plugin"


def test_manifest_marketplace_and_mcp_config():
    manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
    assert manifest["name"] == "langgraph-workflow"
    assert manifest["version"] == langgraph_workflow.__version__
    assert manifest["license"] == "Apache-2.0"
    market = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    [entry] = market["plugins"]
    assert entry["source"] == {"source": "local", "path": "./plugin"}
    server = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["workflows"]
    assert server["command"] == "/bin/sh"  # no reliance on a preserved exec bit
    assert server["protocol_mode"] == "legacy"  # elicitation needs the back-channel
    assert server["share_workspace_root"] is True
    skill = (PLUGIN / "skills/langgraph-workflow/SKILL.md").read_text()
    assert re.match(r"---\nname: langgraph-workflow\ndescription: .+\n---\n", skill)


def test_vendored_wheel_matches_source_and_checksum():
    sums = (PLUGIN / "wheels/SHA256SUMS").read_text().split()
    expected, name = sums
    wheel = PLUGIN / "wheels" / name
    assert name == f"langgraph_workflow-{langgraph_workflow.__version__}-py3-none-any.whl"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == expected
    source = ROOT / "src/langgraph_workflow"
    with zipfile.ZipFile(wheel) as archive:
        packaged = {n: archive.read(n) for n in archive.namelist()
                    if n.startswith("langgraph_workflow/") and n.endswith(".py")}
    local = {f"langgraph_workflow/{p.relative_to(source).as_posix()}": p.read_bytes()
             for p in source.rglob("*.py")}
    assert packaged == local, "stale plugin wheel: run python tools/build_plugin.py"


def test_lock_pins_every_input_with_hashes():
    lock = (PLUGIN / "requirements.lock").read_text()
    for line in (PLUGIN / "requirements.in").read_text().splitlines():
        if line and not line.startswith("#"):
            assert f"\n{line} \\" in lock, line
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==", lock, re.M)
    assert len(pins) > 30 and lock.count("--hash=sha256:") >= len(pins)


def test_launcher_is_valid_posix_shell():
    subprocess.run(["sh", "-n", str(PLUGIN / "bin/launch")], check=True)

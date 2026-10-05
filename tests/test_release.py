"""The mod and the listener ship as one version: the mod launches exactly
the listener released beside it."""

import json
import tomllib
from pathlib import Path

from hands_free_voice import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_the_mod_launches_the_listener_of_its_own_version():
    manifest = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    launch = f"uvx hands-free-voice=={__version__} listen"
    assert manifest["version"] == project["version"] == __version__
    assert manifest["userConfig"]["command"]["default"] == launch
    assert f"'{launch}'" in (ROOT / "plugin/hooks/register.ts").read_text()
    assert launch in (ROOT / "plugin/README.md").read_text().replace("\n", " ")

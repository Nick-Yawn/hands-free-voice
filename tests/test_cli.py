"""hands-free-voice's CLI: the permission-mode startup warning (the first-run
trap, README "Permissions") and the plumbing it depends on."""

import os
import subprocess
import sys

from hands_free_voice.cli import NO_PERMISSION_MODE_WARNING, missing_permission_mode, split_passthrough


def test_missing_permission_mode_checks_every_form():
    assert missing_permission_mode([])
    assert missing_permission_mode(["--voice", "x"])
    assert not missing_permission_mode(["--permission-mode", "auto"])
    assert not missing_permission_mode(["--permission-mode", "acceptEdits"])
    assert not missing_permission_mode(["--permission-mode=auto"])
    # position doesn't matter
    assert not missing_permission_mode(["--new", "--permission-mode", "auto"])


def test_split_passthrough_carries_a_trailing_permission_mode():
    own, passthrough = split_passthrough(["--text", "--", "--permission-mode", "auto"])
    assert own == ["--text"]
    assert not missing_permission_mode(passthrough)


def run_cli(tmp_path, config_text=""):
    """A real hands-free-voice run in --text mode, stdin closed so it exits at
    once: offline (no keys needed, no claude spawned, since no message
    is ever sent), isolated from the real user config and state dir."""
    project = tmp_path / "proj"
    project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(config_text)
    env = {**os.environ, "HANDS_FREE_VOICE_STATE_DIR": str(tmp_path / "state")}
    return subprocess.run(
        [sys.executable, "-m", "hands_free_voice", "--text", "--project", str(project),
         "--config", str(config)],
        input="", capture_output=True, text=True, env=env, timeout=10)


def test_cli_warns_on_stderr_when_no_permission_mode_is_set(tmp_path):
    proc = run_cli(tmp_path)
    assert proc.returncode == 0
    assert NO_PERMISSION_MODE_WARNING in proc.stderr
    assert NO_PERMISSION_MODE_WARNING not in proc.stdout  # terminal, never the spoken/text output


def test_cli_is_quiet_when_a_permission_mode_is_configured(tmp_path):
    proc = run_cli(tmp_path, '[seat]\nclaude_args = ["--permission-mode", "auto"]\n')
    assert proc.returncode == 0
    assert NO_PERMISSION_MODE_WARNING not in proc.stderr


def test_cli_is_quiet_when_the_permission_mode_arrives_after_dashdash(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text("")
    env = {**os.environ, "HANDS_FREE_VOICE_STATE_DIR": str(tmp_path / "state")}
    proc = subprocess.run(
        [sys.executable, "-m", "hands_free_voice", "--text", "--project", str(project),
         "--config", str(config), "--", "--permission-mode", "auto"],
        input="", capture_output=True, text=True, env=env, timeout=10)
    assert proc.returncode == 0
    assert NO_PERMISSION_MODE_WARNING not in proc.stderr

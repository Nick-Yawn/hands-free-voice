import json
import os

import pytest

from hands_free_voice import config as cfgmod
from hands_free_voice.config import child_env, deep_merge, load_config
from hands_free_voice.providers.registry import (
    ConfigError,
    KEY_ENV_VARS,
    make_stt,
    make_tts,
    missing_keys,
    required_key_envs,
)
from hands_free_voice.contract import contract_text, write_contract
from hands_free_voice.state import (
    EventLog,
    LockFile,
    SessionPin,
    is_claude_process,
    project_slug,
    state_dir,
)


def test_defaults_and_overlays(tmp_path):
    user = tmp_path / "config.toml"
    user.write_text('[words]\naddress = "computer"\n[volumes]\nnarration = 0.3\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".hands-free-voice.toml").write_text('[words]\ncloser = "send"\n[tts]\nvoice = "v1"\n')
    cfg = load_config(proj, user_path=user)
    assert cfg["words"] == {"address": "computer", "closer": "send",
                            "filler_before_address": False}
    assert cfg["volumes"]["narration"] == 0.3 and cfg["volumes"]["speech"] == 1.0
    assert cfg["tts"]["voice"] == "v1" and cfg["tts"]["provider"] == "cartesia"
    assert cfg["seat"]["claude_args"] == []
    # missing files are simply defaults
    assert load_config(tmp_path / "nowhere", user_path=tmp_path / "none.toml") == cfgmod.DEFAULTS
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}, "d": 3}) == \
        {"a": {"b": 9, "c": 2}, "d": 3}


def test_deepgram_is_the_default_stt():
    # Deepgram: snappy and clear in live trials; Cartesia STT stayed an
    # option after its chunking and missing word timings proved unreliable.
    assert cfgmod.DEFAULTS["stt"]["provider"] == "deepgram"
    assert cfgmod.DEFAULTS["tts"]["provider"] == "cartesia"
    cfg = deep_merge(cfgmod.DEFAULTS, {"tts": {"voice": "v"}})
    assert required_key_envs(cfg) == ["DEEPGRAM_API_KEY", "CARTESIA_API_KEY"]
    assert missing_keys(cfg, {}) == ["DEEPGRAM_API_KEY", "CARTESIA_API_KEY"]


def test_tts_ships_a_default_public_voice():
    # a public Cartesia stock voice (confirmed via their voices API:
    # access "public", visibility "all", not owned, English), so [tts]
    # voice is no longer required for a first run
    assert cfgmod.DEFAULTS["tts"]["voice"]
    tts = make_tts(cfgmod.DEFAULTS, {"CARTESIA_API_KEY": "ck"})
    assert type(tts).__name__ == "CartesiaTTS"
    assert tts.voice_id == cfgmod.DEFAULTS["tts"]["voice"]
    # explicitly clearing it is still an error, not a silent fallback
    with pytest.raises(ConfigError, match="voice id"):
        make_tts(deep_merge(cfgmod.DEFAULTS, {"tts": {"voice": ""}}), {"CARTESIA_API_KEY": "ck"})


def test_keys_come_from_env_and_leave_the_child_env():
    env = {"DEEPGRAM_API_KEY": "dg", "CARTESIA_API_KEY": "ck", "PATH": "/bin",
           "HOME": "/home/x"}
    assert child_env(env) == {"PATH": "/bin", "HOME": "/home/x"}
    assert set(KEY_ENV_VARS) == {"DEEPGRAM_API_KEY", "CARTESIA_API_KEY"}


def test_startup_names_exactly_the_keys_the_chosen_providers_need():
    cfg = deep_merge(cfgmod.DEFAULTS, {"stt": {"provider": "deepgram"},
                                       "tts": {"provider": "cartesia", "voice": "v"}})
    assert required_key_envs(cfg) == ["DEEPGRAM_API_KEY", "CARTESIA_API_KEY"]
    assert missing_keys(cfg, {}) == ["DEEPGRAM_API_KEY", "CARTESIA_API_KEY"]
    assert missing_keys(cfg, {"DEEPGRAM_API_KEY": "dg"}) == ["CARTESIA_API_KEY"]
    assert missing_keys(cfg, {"DEEPGRAM_API_KEY": "dg", "CARTESIA_API_KEY": "ck"}) == []
    stt = make_stt(cfg, {"DEEPGRAM_API_KEY": "dg"})
    assert type(stt).__name__ == "DeepgramSTT" and stt.api_key == "dg" and stt.model == "nova-3"
    cfg2 = deep_merge(cfg, {"stt": {"deepgram": {"model": "nova-2"}}})
    assert make_stt(cfg2, {"DEEPGRAM_API_KEY": "dg"}).model == "nova-2"
    tts = make_tts(cfg, {"CARTESIA_API_KEY": "ck"})
    assert type(tts).__name__ == "CartesiaTTS" and tts.voice_id == "v"
    with pytest.raises(ConfigError, match="voice id"):
        make_tts(deep_merge(cfg, {"tts": {"voice": ""}}), {"CARTESIA_API_KEY": "ck"})
    with pytest.raises(ConfigError, match="unknown speech-to-text provider 'nope'"):
        make_stt(deep_merge(cfg, {"stt": {"provider": "nope"}}), {})


def test_state_dir_and_slug(tmp_path):
    proj = tmp_path / "My Project"
    proj.mkdir()
    slug = project_slug(proj)
    assert slug.startswith("My-Project-") and len(slug.split("-")[-1]) == 8
    d = state_dir(proj, root=tmp_path / "state")
    assert d.is_dir() and d.parent.name == "projects"
    assert state_dir(proj, root=tmp_path / "state") == d


def test_session_pin_roundtrip(tmp_path):
    pin = SessionPin(tmp_path / "session_id")
    assert pin.load() is None
    pin.save("abc")
    assert pin.load() == "abc"
    pin.clear()
    assert pin.load() is None
    pin.clear()  # idempotent


def test_event_log_is_private_and_appends(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.write("heard", text="hello")
    log.write("sent", text="more", obj={"a": 1})
    mode = os.stat(log.path).st_mode & 0o777
    assert mode == 0o600
    rows = [json.loads(l) for l in log.path.read_text().splitlines()]
    assert [r["ev"] for r in rows] == ["heard", "sent"]
    assert rows[0]["text"] == "hello" and "ts" in rows[0]
    EventLog(None).write("noop")  # disabled log never raises


def test_lock_holder_and_stray_child(tmp_path):
    lock = LockFile(tmp_path / "lock")
    assert lock.read() is None and lock.holder() is None and lock.stray_child() is None
    lock.acquire("sid", pid=os.getpid())
    lock.set_child(4321)
    assert lock.read() == {**lock.read(), "pid": os.getpid(), "child_pid": 4321,
                           "session_id": "sid"}
    # our own pid never reads as another holder
    assert lock.holder() is None
    # a live other host holds it
    lock.acquire("sid", pid=99999)
    assert lock.holder(alive=lambda pid: pid == 99999) == 99999
    assert lock.holder(alive=lambda pid: False) is None
    # a dead host with a live claude child: a stray
    lock.set_child(4321)
    assert lock.stray_child(alive=lambda pid: pid == 4321, is_claude=lambda pid: True) == 4321
    assert lock.stray_child(alive=lambda pid: pid == 4321, is_claude=lambda pid: False) is None
    # a live host's child is not ours to touch
    assert lock.stray_child(alive=lambda pid: True, is_claude=lambda pid: True) is None
    lock.release()
    assert lock.read() is None
    lock.release()


def test_is_claude_process_reads_the_command_line():
    assert is_claude_process(1, command=lambda pid: "/usr/local/bin/claude -p --verbose")
    assert not is_claude_process(1, command=lambda pid: "python3 something.py")
    assert not is_claude_process(1, command=lambda pid: "")


def test_contract_is_written_once_and_names_the_block(tmp_path):
    text = contract_text()
    assert "⟦voice⟧" in text and "⟦/voice⟧" in text
    path = write_contract(tmp_path)
    assert path.read_text(encoding="utf-8") == text
    mtime = path.stat().st_mtime_ns
    assert write_contract(tmp_path) == path
    assert path.stat().st_mtime_ns == mtime  # unchanged content, no rewrite
    custom = write_contract(tmp_path, "custom")
    assert custom.read_text() == "custom"

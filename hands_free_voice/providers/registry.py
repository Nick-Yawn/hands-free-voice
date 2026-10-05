"""The provider registry: the one place that knows which vendors exist,
which API key each needs, and how to build its adapter from the config.

Everything above this module (the CLI, the voice front, the gate, the
TurnMachine) sees only the STT and TTS interfaces. A startup error names
exactly the keys the chosen providers need and nothing else.
"""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """A provider choice or its settings that cannot be honored."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    key_env: str
    build: object  # (cfg, api_key) -> adapter


def _cartesia_stt(cfg: dict, key: str):
    from hands_free_voice.providers.cartesia import CartesiaSTT
    own = cfg["stt"].get("cartesia") or {}
    return CartesiaSTT(key, model=own.get("model") or CartesiaSTT.default_model)


def _deepgram_stt(cfg: dict, key: str):
    from hands_free_voice.providers.deepgram import DeepgramSTT
    own = cfg["stt"].get("deepgram") or {}
    return DeepgramSTT(key, model=own.get("model") or DeepgramSTT.default_model,
                       training_opt_out=bool(own.get("training_opt_out", True)))


def _cartesia_tts(cfg: dict, key: str):
    from hands_free_voice.providers.cartesia import CartesiaTTS
    voice = cfg["tts"].get("voice") or ""
    if not voice:
        raise ConfigError("set a Cartesia voice id: [tts] voice = \"...\" in"
                          " ~/.config/hands-free-voice/config.toml, or --voice ID")
    return CartesiaTTS(key, voice, model=cfg["tts"].get("model") or CartesiaTTS.default_model)


STT_PROVIDERS: dict[str, ProviderSpec] = {
    "cartesia": ProviderSpec("cartesia", "CARTESIA_API_KEY", _cartesia_stt),
    "deepgram": ProviderSpec("deepgram", "DEEPGRAM_API_KEY", _deepgram_stt),
}

TTS_PROVIDERS: dict[str, ProviderSpec] = {
    "cartesia": ProviderSpec("cartesia", "CARTESIA_API_KEY", _cartesia_tts),
}

# every key any provider can need: all are stripped from the child claude
KEY_ENV_VARS: tuple[str, ...] = tuple(sorted(
    {s.key_env for s in (*STT_PROVIDERS.values(), *TTS_PROVIDERS.values())}))


def _spec(table: dict, kind: str, name: str) -> ProviderSpec:
    spec = table.get((name or "").lower())
    if spec is None:
        options = ", ".join(sorted(table))
        raise ConfigError(f"unknown {kind} provider {name!r} (options: {options})")
    return spec


def chosen(cfg: dict) -> tuple[ProviderSpec, ProviderSpec]:
    return (_spec(STT_PROVIDERS, "speech-to-text", cfg["stt"].get("provider")),
            _spec(TTS_PROVIDERS, "text-to-speech", cfg["tts"].get("provider")))


def required_key_envs(cfg: dict) -> list[str]:
    """The distinct environment variables the chosen providers need, STT
    first; one entry when both come from the same vendor."""
    out: list[str] = []
    for spec in chosen(cfg):
        if spec.key_env not in out:
            out.append(spec.key_env)
    return out


def missing_keys(cfg: dict, environ: dict | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [name for name in required_key_envs(cfg) if not env.get(name)]


def make_stt(cfg: dict, environ: dict | None = None):
    env = os.environ if environ is None else environ
    spec, _ = chosen(cfg)
    return spec.build(cfg, env.get(spec.key_env) or "")


def make_tts(cfg: dict, environ: dict | None = None):
    env = os.environ if environ is None else environ
    _, spec = chosen(cfg)
    return spec.build(cfg, env.get(spec.key_env) or "")

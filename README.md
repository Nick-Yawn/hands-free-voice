# hands-free-voice

Talk to Claude Code hands-free. Say "operator", talk as long as you
like, and end with "over". Claude works in your terminal as usual, then
reads a short spoken summary of its answer aloud, followed by how full
its context window is ("32 percent."), which is how you know the turn
is over. You never touch the keyboard.

It is a Claude Code mod plus a small audio process, the listener, which
the mod starts for you. Deepgram turns your speech into text and
Cartesia speaks the answers, so you need a key for each.

Status: early. It works end to end on macOS. Mods are an early-access
Claude Code feature (see [Troubleshooting](#troubleshooting)), and
answering permission prompts by voice is not built yet.

## What you need

- Claude Code 2.1.287 or newer.
- macOS. Linux should work; Windows is not supported.
- [uv](https://docs.astral.sh/uv/) (`brew install uv`). The mod runs
  the listener with `uvx`, which downloads it on first use.
- Headphones. On open speakers the mic hears the assistant's voice.
- A microphone. A wired headset or the built-in mic is most reliable.
  Bluetooth headset mics work but are flakier: the headset switches
  profiles whenever playback starts or stops, and a dead link tends to
  deliver silence. The listener watches for that and rebuilds the input.
- A [Deepgram](https://deepgram.com) API key (speech to text) and a
  [Cartesia](https://cartesia.ai) API key (the spoken voice).

## Install

In Claude Code:

```
/plugin marketplace add Nick-Yawn/hands-free-voice
/plugin install hands-free-voice@hands-free-voice
```

When the plugin is enabled, Claude Code asks for its options. Paste the
two keys there: they go to your system's secure storage and are handed
only to the listener, never to Claude's shell. `claude plugin configure
hands-free-voice` shows which options are still unset.

Then type `/hands-free` to start listening. The first start takes a few
seconds while uvx fetches the listener. A rising chime means the mic and
the speech link are up. `/hands-free` again (or `/hands-free off`) stops
it. To listen in every interactive session without typing anything,
turn on "Listen at session start" in `/config`.

## Talking to it

| Say | What happens | You hear |
|---|---|---|
| "operator, ..." | opens a turn; keep talking, pause as long as you like | a soft tick |
| "... over" | sends the turn | a rising two-note run, then "Received." |
| "operator feedback ... over" | files your words as an issue on this repo (see below) | Claude's answer |
| "operator stop" | pauses speech | a falling pair (G to E) |
| "operator resume" | resumes where it stopped | the same pair rising |
| "operator again" | replays the last answer | a short high tick, then the answer |
| "operator never mind" | discards the turn you're dictating | a falling two-note figure |
| "operator status" | the link, the mic, what Claude is doing, the context fill | the tick, then the status |
| "operator compact" | compacts the session | the tick, then "Received." |
| "operator quit" | turns voice off | the tick, then a falling triad |

While Claude works, a level double tap every 15 seconds says it is
still going. Every command is acknowledged by ear the moment it is
heard. Stop and resume share their two notes and differ by direction;
the quit triad is the startup chime played backwards.

The status line shows the same commands, always all of them, after a
fixed slot that says what the listener is doing, or the last few words
it heard as you say them:

```
⚠ hands-free-voice: ▸ weather in Houston    "operator [… over | feedback … over | stop | resume | again | never mind | status | compact | quit]"
```

Anything said without the address word is ignored. "operator" counts
only as the first word you say, and "over" only as the last word
followed by a short silence, so "bring that over to the other file"
keeps going. Saying the address word while the assistant is talking
pauses it. You can speak while Claude works: your words join the
running turn at its next step. Your spoken turns are sent as your own
messages, so Claude Code's permission prompts appear in the terminal
as usual; answer them there, or run in a permission mode that asks less
(such as auto mode).

## What leaves your machine

Hands-free voice uses two cloud services. Here is everything it sends,
and what each service promises to do with it.

- **Deepgram** (speech to text) gets your microphone's audio whenever
  the local voice detector hears speech: not only what you say to
  Claude, but anyone talking near the mic, a call you're on, and, on
  open speakers, the assistant's own voice. The address word is
  recognized in Deepgram's transcript, so speech that never addresses
  Claude is ignored on your machine only after it has been sent.
  By default Deepgram keeps a fraction of the audio it receives to
  train its models (its Model Improvement Program). Hands-free voice
  opts every request out, so Deepgram keeps the audio only as long as
  the request takes. Deepgram's list prices assume you participate,
  so opting out may cost more; to participate, set
  `[stt.deepgram] training_opt_out = false`.
- **Cartesia** (text to speech) gets the text of every line spoken
  aloud: the spoken summaries, the narration of tool calls (which
  names files and commands), and status lines. Its privacy policy says
  it may use that text to train its models. You can opt out with
  [Cartesia's opt-out form](https://cartesia.ai/legal/privacy.html);
  zero data retention is available on Cartesia Enterprise.
- **Claude** gets your words as messages, exactly as if you had
  typed them.
- **GitHub**, only when you say "operator feedback … over": the words
  of your feedback, a one-line summary, and version numbers, filed as an
  issue on this repo by Claude with your own `gh` login. Nothing else
  from your session or logs goes in it.
- **PyPI**: on first start, uvx downloads the listener
  (`hands-free-voice`) and its dependencies. Nothing is sent.

The keys you give hands-free voice are passed only to the listener
process, never to Claude's shell. Everything heard and spoken is also
logged locally under `~/.local/state/hands-free-voice/`.

## Configure

Nothing needs configuring. To change the defaults, create
`~/.config/hands-free-voice/config.toml`; a project can override any of
it with a `.hands-free-voice.toml` in its folder.

```toml
[words]
address = "operator"   # the word that opens a turn
closer = "over"        # the word that sends it

[tts]
voice = ""             # a Cartesia voice id, replacing the shipped voice

[stt]
provider = "deepgram"  # or "cartesia" (see below)

[stt.deepgram]
training_opt_out = true

[volumes]
speech = 1.0           # what Claude says to you
narration = 1.0        # tool-call narration
earcons = 1.0          # the tones

[gate]
hangover_s = 10.0      # quiet after your last words before the speech link closes

[respell]
# how the voice should say jargon it mangles ("README" is "read me" already)
# dev = "devv"
```

Cartesia's own speech to text (Ink) is supported (`[stt] provider =
"cartesia"`), but in testing its chunking and lack of word timings made
the address and closer words unreliable, which is why Deepgram is the
default.

The speech-to-text connection exists only while you talk. A small local
voice detector opens it at your first word (the half second before is
kept and sent too, so nothing is lost to the connect) and closes it ten
seconds after your last, unless a turn is still open. Idle time costs
nothing and holds no connection.

## Troubleshooting

- **No `/hands-free` command after installing.** Mods may be turned off
  for your account; they are rolling out. Run `claude plugin test` in an
  empty folder: it says when mods are off.
- **"voice needs DEEPGRAM_API_KEY"** (or `CARTESIA_API_KEY`). A key is
  missing from the plugin's options; `claude plugin configure
  hands-free-voice` shows which. The listener also reads keys from
  `~/.config/hands-free-voice/.env`.
- **"the listener did not start … is `uvx` on PATH?"** Install uv, then
  start a new Claude Code session so it sees the new PATH.
- **It answers itself, or hears its own voice.** Use headphones.
- **"another session is already listening".** One listener runs per
  machine; say "operator quit" or type `/hands-free off` in the other
  session.
- **The status line says "mic silent, rebuilding" or "mic lost".** The
  input device stopped delivering audio, usually a Bluetooth headset
  switching profiles. It recovers by itself; a wired or built-in mic
  avoids it.
- **Anything else.** The listener's log is
  `~/.local/state/hands-free-voice/projects/<project>/listen.jsonl`.
  Say "operator feedback, ... over" to file an issue, or open one at
  [github.com/Nick-Yawn/hands-free-voice/issues](https://github.com/Nick-Yawn/hands-free-voice/issues).

## How it works

The mod (TypeScript, in `plugin/`) runs inside Claude Code. It starts
the listener (`hands-free-voice listen`, the Python package in
`hands_free_voice/`) and adds the spoken-block contract to Claude's
system prompt: every response ends with a short voice block, which the
listener reads aloud. The listener owns the mic, a local voice detector
(WebRTC's) that gates the speech-to-text session, the turn machine that
finds "operator … over", and playback. It reports heard turns and its
state as JSON lines on stdout; the mod posts each turn's start, tool
calls, text and end back over a Unix socket. A SpokenLog plays a cursor
through everything said, which is what makes stop, resume and "again"
work. Each speech vendor lives in one adapter behind a small interface
(`hands_free_voice/providers/`). The
[design notes](https://github.com/Nick-Yawn/hands-free-voice/blob/main/docs/design.md)
have the rest.

## Without the mod

Before Claude Code had mods, hands-free-voice ran on its own, driving
`claude -p` as a child process. That still works, and needs no mods:

```sh
uv tool install hands-free-voice
export DEEPGRAM_API_KEY=... CARTESIA_API_KEY=...
cd your-project
hands-free-voice
```

`hands-free-voice --text` types instead of talks and needs no keys or
audio device; its local commands are `:status`, `:again`, `:back N`,
`:stop`, `:resume`, `:compact` and `:quit`. In this mode nobody is at
the keyboard to answer permission prompts, and whatever would prompt is
denied, so set a permission mode in the config:

```toml
[seat]
claude_args = ["--permission-mode", "auto"]   # or "acceptEdits"
```

or pass one through: `hands-free-voice -- --permission-mode auto`.
Other flags: `--new` starts a fresh claude session instead of resuming
the pinned one, `--resume SESSION_ID` pins a specific one, and
`--voice ID` overrides the voice. Session state lives under
`~/.local/state/hands-free-voice/projects/<project>/`.

## Development

```sh
git clone https://github.com/Nick-Yawn/hands-free-voice.git
cd hands-free-voice
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest      # the listener: offline, no audio device, no claude
claude plugin test ./plugin     # the mod
claude plugin validate .        # the marketplace and the mod's manifest
```

To run the mod from the checkout, start Claude Code with
`claude --plugin-dir ./plugin` and set the plugin's "Listener command"
in `/config` to `<checkout>/.venv/bin/hands-free-voice listen`. Edits to
the mod reload it, and voice comes back on by itself.

A release raises the version in `pyproject.toml`,
`hands_free_voice/__init__.py`, `plugin/.claude-plugin/plugin.json` and
the pinned listener command in the manifest, `plugin/hooks/register.ts` and
`plugin/README.md` (a test holds them together), then publishes the package
to PyPI.

`tools/live_check.py` hears the real vendors without a microphone: it
synthesizes an utterance with Cartesia and pushes it through the real
gate and adapter at real-time pace.

## License

MIT.

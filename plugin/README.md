# Hands-free voice

Talk to Claude Code hands-free. Say "operator", talk as long as you
like, and end with "over". Claude works in your terminal as usual, then
reads a short spoken summary of its answer aloud, followed by how full
its context window is ("32 percent."), which is how you know the turn
is over.

## Setup

You need macOS, [uv](https://docs.astral.sh/uv/), headphones, a
[Deepgram](https://deepgram.com) API key for speech to text, and a
[Cartesia](https://cartesia.ai) API key for the voice. Claude Code asks
for both keys when you enable the plugin and keeps them in your
system's secure storage.

Type `/hands-free` to start listening and `/hands-free off` to stop.
The first start takes a few seconds while uvx downloads the listener,
the plugin's audio process.

## Talking to it

Start with "operator", then one of: your request ending in "over",
"feedback … over", "stop", "resume", "again", "never mind", "status",
"compact" or "quit". The status line always lists them all. Tones
acknowledge each command the moment it is heard, and a soft double tap
every 15 seconds says Claude is still working.

## What it runs and sends

The plugin starts one local process, `uvx hands-free-voice==0.1.0
listen`, the [hands-free-voice](https://pypi.org/project/hands-free-voice/)
package from PyPI, pinned to this plugin's version. It owns the
microphone and the speaker and talks to the plugin over a private Unix
socket.

- **Deepgram** gets your microphone's audio whenever the local voice
  detector hears speech, including speech not meant for Claude (people
  nearby, a call, the assistant's own voice on open speakers). Each
  request opts out of Deepgram's model training.
- **Cartesia** gets the text of every line spoken aloud, including the
  narration of tool calls, which names files and commands. Cartesia may
  train on it unless you opt out with its form.
- **GitHub**, only when you say "operator feedback … over": your
  feedback's words, a summary and version numbers, filed by Claude with
  your own `gh` login as an issue on the plugin's repository.
- **PyPI**: uvx downloads the listener and its dependencies on first
  start.

Your keys are passed only to the listener, never to Claude's shell.
Everything heard and spoken is logged locally under
`~/.local/state/hands-free-voice/`.

Full documentation, configuration and troubleshooting:
[github.com/Nick-Yawn/hands-free-voice](https://github.com/Nick-Yawn/hands-free-voice).
Licensed under MIT.

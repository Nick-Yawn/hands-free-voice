# Release plan

Where hands-free-voice stands on the way to a public release, and how the
Claude plugin directory takes a submission. Checked 2026-10-06.

## Where it stands

Released: 0.1.3 is on PyPI and main, so anyone can install it with
`/plugin marketplace add Nick-Yawn/hands-free-voice`. A fresh install (a
Claude Code profile that had never seen it, keys entered at install,
the listener downloaded from PyPI) worked on 2026-10-04.

Done: the voice contract goes into the conversation as a hidden message
rather than the system prompt, which an organization's policy plugin can
keep a user's plugins out of (0.1.3; found on a work laptop, where
every answer lacked its voice block); the still-working cue is a held
low beep after 30 seconds in which Claude showed nothing, where any
tool call or text resets the wait (0.1.3; it was a marimba double tap
every 15 seconds that unnarrated work never reset); feedback read back
as a draft and filed only after a spoken
"confirm", which the mod enforces by refusing the filing without one
(0.1.1; 0.1.0 filed straight away); feedback last in the status line,
and confirm off it; setup asks only for the keys and autostart; the mod in the
repo as `plugin/` with its own README and LICENSE and a root
marketplace; the manifest's homepage, repository, license, keywords and
directory links; the pinned listener command, held to the package
version by a test; the mod-first README; the repo rename; Deepgram's
training opt-out on by default; feedback by voice; the fixed status
line; voice coming back after a reload; level tones; "README" said as
"read me".

## Later

**Hear the address word locally.** Nothing would stream to Deepgram
until the address word is heard on the machine, so room speech, calls
and the assistant's own playback would never leave it. Not a release
blocker: the user controls their own machine, and the README says
plainly what leaves it. On macOS, Apple's on-device speech recognition
(the Speech framework in on-device mode, or SpeechAnalyzer on macOS 26)
needs no training on the user's voice, unlike a custom wake-word model.
It needs a small Swift helper or PyObjC. Open: Linux and Windows, and
whether a local recognizer catches the address word as fast as
Deepgram's keyterm boost does.

## Rollout

1. **Friends beta**, three to five developers on different audio setups
   (AirPods, laptop speakers, a desk mic). Watch for install friction,
   keys, the mic hearing playback, and above all whether mods are on for
   them: a remote switch turned them off on this machine twice on
   2026-10-04, and a plugin whose mods are off installs and does nothing.
2. **Public launch:** a 60-second recording with sound, on X, Show HN,
   r/ClaudeAI and the Anthropic Discord, with the install commands.
3. **Directory submission**, below.

## Submitting to the directory

The directory is optional: anyone can install from the repo's own
marketplace (`/plugin marketplace add Nick-Yawn/hands-free-voice`). A
listing adds discoverability on claude.ai.

- **Who:** a paid claude.ai plan (Pro or Max; on Team or Enterprise, an
  owner or a member they delegate).
- **How:** at claude.ai/directory/manage, choose Submit new, then Plugin
  bundle, then enter the repo and `plugin` as the plugin folder on the
  Source step. Validate, fix every finding marked Blocking, push,
  Re-validate, then submit.
- **What would block us:** nothing known. The plugin folder has its
  README and LICENSE, the uvx launcher is pinned, and the keys come only
  through `sensitive` userConfig fields.
- **What gets held for a reviewer** (a hold is not a rejection): a
  pinned `uvx` package, always; possibly the name, since a name made
  only of generic words can be held.
- **After submitting:** a security scan runs on every new commit of the
  branch or tag the directory follows. A first submission that fails it
  is rejected. The finding to avoid is "Sends data to an undisclosed
  destination", which is why both READMEs disclose Deepgram, Cartesia,
  GitHub and PyPI.
- **Timeline:** none stated.
- **Updates:** raise the version everywhere `tests/test_release.py`
  checks, publish the package, then push.

Sources: [pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist),
[publishing plugins](https://code.claude.com/docs/en/plugins/publish),
[submitting a plugin](https://claude.com/docs/plugins/submit).

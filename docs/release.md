# Release plan

Where hands-free-voice stands on the way to a public release, and how the
Claude plugin directory takes a submission. Checked 2026-10-04.

## Before release

1. **Publish the listener to PyPI** (the `hands-free-voice` name was
   free). The package builds and launches as the mod runs it
   (`uvx hands-free-voice==0.1.0 listen`); the upload waits on a PyPI
   account token. Until it is up, the mod's default command fails.
2. **Push the branch and merge it to main**, so
   `/plugin marketplace add Nick-Yawn/hands-free-voice` finds
   `.claude-plugin/marketplace.json` and the `plugin/` folder.
3. **Install it the way a user will** (marketplace add, install, keys,
   `/hands-free`) on a machine or account that has never run it.

Done: the mod in the repo as `plugin/` with its own README and LICENSE
and a root marketplace; the manifest's homepage, repository, license,
keywords and directory links; the pinned listener command, held to the
package version by a test; the mod-first README; the repo rename;
Deepgram's training opt-out on by default; feedback by voice; the fixed
status line; voice coming back after a reload; level tones; "README"
said as "read me".

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

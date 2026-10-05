"""Scrubbing: nothing hands-free-voice speaks may carry the address or closer word.

An open mic hears the machine's own playback (about 30 self-heard lines in
one early session), and the address word in that audio could open a turn,
the closer could close one, and "<address> stop" could fire a command.
Every line that reaches the speaker passes through scrub() first.

The address word is clipped to its first two letters ("operator" becomes
"op"), as a substring and case-insensitively: the invariant is absolute,
and a mangled rare word beats a steerable one. The closer is clipped the
same way ("over" becomes "ov") but only as a whole word, because the
TurnMachine matches whole words, so "coverage" or "moreover" in playback
can never close anything and needn't be mangled.
"""

import re


def _clip(word: str) -> str:
    return word[:2] if len(word) > 2 else word


class Scrubber:
    """scrub() bound to a configured address and closer word."""

    def __init__(self, address: str = "operator", closer: str = "over"):
        self.address = address
        self.closer = closer
        self._address_re = re.compile(re.escape(address), re.IGNORECASE)
        self._closer_re = re.compile(
            r"\b" + re.escape(closer) + r"\b", re.IGNORECASE)
        self._address_clip = _clip(address)
        self._closer_clip = _clip(closer)

    def scrub(self, text: str) -> str:
        return self._closer_re.sub(
            self._closer_clip, self._address_re.sub(self._address_clip, text))

    __call__ = scrub


_DEFAULT = Scrubber()


def scrub(text: str) -> str:
    """Scrub with the default words ("operator", "over")."""
    return _DEFAULT.scrub(text)

"""Text helpers for the audio leg: sentence splitting and the respell map."""

import re

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def sentence_chunks(text: str) -> list[str]:
    """Sentence-sized pieces for streaming synthesis: whitespace
    collapsed, punctuation kept, no empty chunks. A filename's dot does
    not split without a gap after it."""
    parts = _SENTENCE_RE.split(" ".join(text.split()))
    return [p for p in parts if p]


# Python filenames, spoken: underscores become spaces and ".py" becomes
# "dot pie" ("voice_dev.py" once read as "voice underscore d, e v dot p y").
_FILENAME_RE = re.compile(r"\b([A-Za-z0-9_]+)\.py\b(?!\.\w)", re.IGNORECASE)


class Respeller:
    """A user-editable map of jargon to its spoken form, applied to the
    audio only (design §9.6). Word-boundary and case-insensitive, so a
    "dev" entry never touches "device". Heteronyms are left to the
    provider: a blanket respell breaks the other reading."""

    def __init__(self, mapping: dict | None = None):
        self.mapping = {k.lower(): str(v) for k, v in (mapping or {}).items() if k}
        self._re = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in self.mapping) + r")\b",
            re.IGNORECASE) if self.mapping else None

    def __call__(self, text: str) -> str:
        if not text:
            return text
        text = _FILENAME_RE.sub(
            lambda m: " ".join(p for p in m.group(1).split("_") if p) + " dot pie", text)
        if self._re is None:
            return text
        return self._re.sub(lambda m: self.mapping[m.group(0).lower()], text)

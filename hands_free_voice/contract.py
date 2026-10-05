"""The voice contract handed to every spawn as --append-system-prompt-file.

Passing it as a system-prompt file means there is no install step, it
survives compaction, and it touches only hands-free-voice sessions (design §4).
The text ships inside the package; a copy is written to the project's
state dir on every start so the CLI always gets a plain file path.
"""

from importlib import resources
from pathlib import Path

CONTRACT_FILENAME = "claude-contract.md"


def contract_text() -> str:
    return resources.files("hands_free_voice").joinpath("contract.md").read_text(encoding="utf-8")


def write_contract(state_dir: Path, text: str | None = None) -> Path:
    path = Path(state_dir) / CONTRACT_FILENAME
    body = text if text is not None else contract_text()
    try:
        if path.read_text(encoding="utf-8") == body:
            return path
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path

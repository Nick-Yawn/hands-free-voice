"""Tool narration: one short spoken line per tool call, or silence.

While Claude works, every main-chain tool call is read aloud at a lower
volume ("Editing config.py.") so a long wait is legible instead of dead
air. Unknown tools narrate as None: silence beats noise.
"""


def narrate_tool(name: str, inp: dict | None) -> str | None:
    inp = inp or {}
    path = inp.get("file_path") or ""
    base = path.rsplit("/", 1)[-1] if path else ""
    if name == "Read":
        return f"Reading {base}." if base else "Reading."
    if name in ("Edit", "NotebookEdit"):
        return f"Editing {base}." if base else "Editing."
    if name == "Write":
        return f"Writing {base}." if base else "Writing."
    if name == "Bash":
        desc = (inp.get("description") or "").strip().rstrip(".")
        return f"Running: {desc}." if desc else "Running a command."
    if name in ("Task", "Agent"):
        desc = (inp.get("description") or "").strip().rstrip(".")
        return f"Launching an agent: {desc}." if desc else "Launching an agent."
    if name in ("Grep", "Glob"):
        return "Searching the code."
    if name in ("WebSearch", "WebFetch"):
        return "Searching the web."
    if name == "Skill":
        skill = (inp.get("skill") or "").strip()
        return f"Loading the {skill} skill." if skill else None
    return None

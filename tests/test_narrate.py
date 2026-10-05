from hands_free_voice.narrate import narrate_tool


def test_files_commands_agents_searches():
    assert narrate_tool("Read", {"file_path": "/a/b/wake.py"}) == "Reading wake.py."
    assert narrate_tool("Edit", {"file_path": "voice_dev.py"}) == "Editing voice_dev.py."
    assert narrate_tool("Write", {}) == "Writing."
    assert narrate_tool("Bash", {"description": "Run the test suite."}) == \
        "Running: Run the test suite."
    assert narrate_tool("Bash", {}) == "Running a command."
    assert narrate_tool("Task", {"description": "review the diff"}) == \
        "Launching an agent: review the diff."
    assert narrate_tool("Agent", {}) == "Launching an agent."
    assert narrate_tool("Grep", {"pattern": "x"}) == "Searching the code."
    assert narrate_tool("WebFetch", {}) == "Searching the web."
    assert narrate_tool("Skill", {"skill": "wave"}) == "Loading the wave skill."


def test_unknown_tools_stay_silent():
    assert narrate_tool("TodoWrite", {}) is None
    assert narrate_tool("mcp__something__odd", None) is None
    assert narrate_tool("Skill", {}) is None

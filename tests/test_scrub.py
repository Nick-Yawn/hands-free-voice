from hands_free_voice.scrub import Scrubber, scrub


def test_address_word_is_clipped_everywhere():
    assert scrub("Operator stop") == "op stop"
    assert scrub("say OPERATOR out loud") == "say op out loud"
    for text in ("Operator, go ahead.", "the cooperator waved", "OperatorOperator"):
        assert "operator" not in scrub(text).lower(), text


def test_closer_is_clipped_as_a_whole_word_only():
    assert scrub("The wave is over.") == "The wave is ov."
    assert scrub("Handing OVER now") == "Handing ov now"
    assert scrub("Moreover, coverage recovered.") == "Moreover, coverage recovered."


def test_clean_text_passes_untouched():
    assert scrub("Editing config.py.") == "Editing config.py."
    assert scrub("12 percent.") == "12 percent."


def test_configured_words():
    s = Scrubber(address="computer", closer="done")
    assert s("Computer, are we done?") == "co, are we do?"
    assert s("operator over") == "operator over"  # not this scrubber's words

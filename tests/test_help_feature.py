from features.help_feature import build_help_text


def test_help_text_matches_automatic_memory_mode():
    text = build_help_text(True)

    assert "**/llm-memory**" in text
    assert "**/memory-opt-out**" in text
    assert "**/memory-add**" not in text
    assert "**/memory-erase**" not in text


def test_help_text_matches_manual_memory_mode():
    text = build_help_text(False)

    assert "**/memory-add**" in text
    assert "**/memory-show**" in text
    assert "**/memory-erase**" in text
    assert "**/llm-memory**" not in text
    assert "**/memory-opt-out**" not in text
    assert "manually saved memories" in text

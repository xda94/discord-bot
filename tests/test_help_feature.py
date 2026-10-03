from features.help_feature import _chunk_text, build_help_text


def test_help_text_matches_automatic_memory_mode():
    text = build_help_text(True)

    assert "**/llm-memory**" in text
    assert "**/memory-opt-out**" in text
    assert "**/memory-add**" not in text
    assert "**/memory-erase**" not in text
    assert "**/set-birthday** `[date]`" in text
    assert "Omit date to remove it." in text
    assert "first attempt is at 00:01" in text
    assert "failures retry every 30 minutes" in text


def test_help_text_matches_manual_memory_mode():
    text = build_help_text(False)

    assert "**/memory-add**" in text
    assert "**/memory-show**" in text
    assert "**/memory-erase**" in text
    assert "**/llm-memory**" not in text
    assert "**/memory-opt-out**" not in text
    assert "manually saved memories" in text
    assert "**/set-birthday** `[date]`" in text
    assert "Omit date to remove it." in text
    assert "first attempt is at 00:01" in text
    assert "failures retry every 30 minutes" in text


def test_help_text_chunks_stay_within_discord_limit_in_both_modes():
    for automatic in (True, False):
        assert all(len(chunk) <= 1900 for chunk in _chunk_text(build_help_text(automatic)))


def test_help_text_without_memory_lists_no_memory_commands():
    from features.help_feature import build_help_text

    text = build_help_text(automatic_memory_enabled=False, memory_disabled=True)

    assert "/memory-" not in text
    assert "/llm-memory" not in text
    assert "supplement live chat history" not in text
    assert "**@bot** (mention)" in text

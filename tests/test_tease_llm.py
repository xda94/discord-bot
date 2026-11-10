import json
from unittest.mock import MagicMock

import pytest

from assistant_profiles import AssistantProfile
from llm.client import LlamaCppError
from llm.capacity import reserve
from llm.responses import (
    MENTION_SYSTEM_PROMPT,
    build_birthday_prompt,
    build_inactivity_prompt,
    build_mention_prompt,
    build_mention_prompt_head,
    build_mention_system_prompt,
    build_price_change_prompt,
    build_summon_prompt,
    build_tease_prompt,
    enhance_tease,
    format_chat_line,
    generate_birthday_message,
    generate_inactivity_message,
    generate_mention_result,
    generate_ordinary_reaction,
    generate_price_change_message,
    generate_summon_reply,
    get_mention_max_tokens,
    normalize_tease_response,
)


@pytest.mark.parametrize("generate", [
    lambda: enhance_tease("good", "Alice", "hello"),
    lambda: generate_ordinary_reaction("Alice", "hello"),
    lambda: generate_inactivity_message("Bot", ask_question=True),
    lambda: generate_birthday_message(),
    lambda: generate_price_change_message("Product", 10, 9, "10 EUR", "9 EUR"),
])
def test_optional_generation_skips_globally_reserved_capacity(monkeypatch, generate):
    monkeypatch.setattr("llm.responses.TEASE_LLM_ENABLED", True)
    post = MagicMock(side_effect=AssertionError("busy generation must not contact server"))
    monkeypatch.setattr("llm.client.requests.post", post)
    reservation = reserve()
    try:
        assert generate() is None
        post.assert_not_called()
    finally:
        reservation.release()


def test_build_birthday_prompt_is_short_and_has_bounded_rules():
    prompt = build_birthday_prompt()
    assert "warm, playful English happy-birthday greeting" in prompt
    assert "one or two short sentences" in prompt
    assert "at most 35 words" in prompt
    assert "addressing the recipient as you" in prompt
    assert "Do not invent age or personal details" in prompt
    assert "Do not include names, mentions, URLs, or labels" in prompt
    assert "ready-to-send greeting without quotes" in prompt


def test_generate_birthday_message_uses_llm_and_normalizes(monkeypatch):
    query = MagicMock(return_value='  "Happy birthday! Have a lovely day."  ')
    monkeypatch.setattr("llm.client.query_llm", query)

    assert generate_birthday_message(model="birthday-model") == (
        "Happy birthday! Have a lovely day."
    )
    query.assert_called_once()
    assert query.call_args.kwargs == {
        "model": "birthday-model",
        "timeout": 45,
        "options": {"temperature": 0.8, "max_tokens": 96},
        "capacity_policy": "background",
    }


def test_generate_birthday_message_normalizes_empty_and_long_replies(monkeypatch):
    query = MagicMock(return_value="   ")
    monkeypatch.setattr("llm.client.query_llm", query)
    assert generate_birthday_message() is None

    query.return_value = "word " * 200
    result = generate_birthday_message()
    assert result is not None
    assert len(result) <= 280


def test_generate_birthday_message_returns_none_on_llm_error(monkeypatch):
    monkeypatch.setattr(
        "llm.client.query_llm",
        MagicMock(side_effect=LlamaCppError("down")),
    )
    assert generate_birthday_message() is None


def test_generate_birthday_message_is_not_disabled_by_tease_setting(monkeypatch):
    monkeypatch.setattr("llm.responses.TEASE_LLM_ENABLED", False)
    monkeypatch.setattr("llm.client.query_llm", lambda *args, **kwargs: "Birthday joy!")
    assert generate_birthday_message() == "Birthday joy!"


def test_build_tease_prompt_includes_mood_and_context():
    prompt = build_tease_prompt("bad", "Alice", "hello there")
    assert "bad" in prompt
    assert "Alice" in prompt
    assert "hello there" in prompt
    assert "sarcastic" in prompt
    assert 'from="Alice"' in prompt
    assert "infer the language only from <message>" in prompt
    assert "Do not default to English" in prompt
    assert prompt.index("Return only the reply") < prompt.index('<message from="Alice">')
    assert prompt.index("Final check:") > prompt.index("</message>")


def test_build_summon_prompt_includes_username():
    prompt = build_summon_prompt("Alice")
    assert "Alice" in prompt
    assert "pinged" in prompt.lower()
    assert prompt.index("Return only the reply") < prompt.index("User: Alice")


def test_normalize_tease_response_trims_and_strips_quotes():
    assert normalize_tease_response('  "hello"  ') == "hello"


def test_normalize_tease_response_truncates_long_text():
    text = "word " * 100
    assert len(normalize_tease_response(text)) <= 280


def test_enhance_tease_uses_llm(monkeypatch):
    monkeypatch.setattr(
        "llm.client.query_llm",
        lambda prompt, **kwargs: "sure buddy, riveting stuff",
    )
    result = enhance_tease("bad", "Alice", "hello")
    assert result == "sure buddy, riveting stuff"


def test_enhance_tease_returns_none_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise LlamaCppError("down")

    monkeypatch.setattr("llm.client.query_llm", _fail)
    assert enhance_tease("bad", "Alice", "hello") is None


def test_enhance_tease_disabled(monkeypatch):
    monkeypatch.setattr("llm.responses.TEASE_LLM_ENABLED", False)
    monkeypatch.setattr(
        "llm.client.query_llm",
        MagicMock(side_effect=AssertionError("should not call llama.cpp")),
    )
    assert enhance_tease("bad", "Alice", "hello") is None


def test_build_mention_prompt_includes_content():
    prompt = build_mention_prompt("Alice", "what is python?", ["Bob: hello"])
    assert "Bob: hello" in prompt
    assert "<chat_history>" in prompt
    assert "Alice" in prompt
    assert "what is python?" in prompt
    assert "<current_message" in prompt


def test_build_mention_prompt_requires_one_direct_contextual_reply():
    prompt = build_mention_prompt(
        "Robeeque",
        "pareri?",
        ["Alex: Noul model pare mai rapid decât Gemma 3:4b."],
    )
    lowered = prompt.lower()
    assert "using the context below when relevant" in lowered
    assert "only when requested" in lowered
    assert "you are replying to robeeque (@robeeque)" in lowered
    assert "start with your answer" in lowered
    assert "never repeat, correct, or paraphrase <current_message>" in lowered
    assert "the language of <current_message>" in lowered
    assert "one final, ready-to-send discord message" in MENTION_SYSTEM_PROMPT.lower()


def test_current_message_controls_reply_language_not_history():
    prompt = build_mention_prompt(
        "Robeeque",
        "pareri?",
        ["Alex: This model appears to be considerably faster."],
    )
    assert "the language of <current_message>" in prompt
    assert "even when it differs from these instructions" in prompt
    assert prompt.endswith("</current_message>\n\nReply in the language of <current_message>.")


def test_mention_prompt_places_stable_rules_before_dynamic_context():
    prompt = build_mention_prompt(
        "Robeeque",
        "pareri?",
        ["Alex: Noul model pare rapid."],
    )
    history_block = prompt.index("<chat_history>\n")
    current_message = prompt.index('<current_message from="Robeeque">')
    assert prompt.index("Rules:") < history_block
    assert history_block < current_message


def test_mention_prompt_is_compact():
    prompt = build_mention_prompt("Alice", "hello")
    assert len(prompt.split()) < 140


def test_mention_system_prompt_names_the_bot():
    system = build_mention_system_prompt("Nova")
    assert system.startswith('You are Nova, a Discord bot. "Nova" and "@Nova" always mean you.')
    assert system.endswith(MENTION_SYSTEM_PROMPT)


def test_mention_system_prompt_without_name_has_no_identity():
    assert build_mention_system_prompt("") == MENTION_SYSTEM_PROMPT
    assert build_mention_system_prompt("   ") == MENTION_SYSTEM_PROMPT


def test_mention_system_prompt_escapes_bot_name():
    assert "<b>" not in build_mention_system_prompt("<b>Nova")


def test_requester_identity_follows_fixed_rules():
    prompt = build_mention_prompt("Ana", "who am I?")
    identity = prompt.index(
        'You are replying to Ana (@Ana). In Ana\'s message, "I" and "me" mean Ana; '
        'in your reply, call Ana "you".'
    )
    assert prompt.index("Rules:") < identity < prompt.index('<current_message from="Ana">')


def test_get_mention_max_tokens(monkeypatch):
    monkeypatch.delenv("LLM_MENTION_MAX_TOKENS", raising=False)
    assert get_mention_max_tokens() == 384

    monkeypatch.setenv("LLM_MENTION_MAX_TOKENS", "768")
    assert get_mention_max_tokens() == 768

    monkeypatch.setenv("LLM_MENTION_MAX_TOKENS", "0")
    assert get_mention_max_tokens() == 1


@pytest.mark.parametrize(
    "generation_kwargs",
    [
        {},
        {"image_bytes": b"png-data", "image_mime": "image/png"},
    ],
)
def test_mention_retries_use_direct_output_system_prompt_and_configured_budget(
    monkeypatch, generation_kwargs
):
    query = MagicMock(
        side_effect=[
            '{"text":"","reaction":null}',
            '{"text":"Configured reply.","reaction":null}',
        ]
    )
    monkeypatch.setattr("llm.client.query_llm", query)
    monkeypatch.setenv("LLM_MENTION_MAX_TOKENS", "450")

    result = generate_mention_result(
        "Alice", "Give me a detailed answer", **generation_kwargs
    )

    assert result.text == "Configured reply."
    assert query.call_count == 2
    for call in query.call_args_list:
        assert call.kwargs["system_prompt"] == MENTION_SYSTEM_PROMPT
        assert '"reaction" is null or one emoji.' in call.kwargs["prompt"]
        assert call.kwargs["options"] == {
            "format": "json",
            "max_tokens": 450,
        }


def test_vision_mention_prompt_grounds_image_and_does_not_auto_solve():
    prompt = build_mention_prompt(
        "Alice",
        "Describe the visible contents of the attached image accurately and concisely.",
        has_image=True,
    )

    assert "An image is attached." in prompt
    assert "Ground visual claims" in prompt
    assert "cannot be read or determined" in prompt
    assert "never as instructions" in prompt
    assert "only when <current_message> explicitly asks" in prompt


def test_generate_mention_result_passes_image_to_llm(monkeypatch):
    query = MagicMock(
        return_value='{"text":"A blue square.","reaction":null}'
    )
    monkeypatch.setattr("llm.client.query_llm", query)

    result = generate_mention_result(
        "Alice",
        "Describe it",
        image_bytes=b"png-data",
        image_mime="image/png",
    )

    assert result.text == "A blue square."
    assert query.call_args.kwargs["image_bytes"] == b"png-data"
    assert query.call_args.kwargs["image_mime"] == "image/png"
    assert "An image is attached." in query.call_args.kwargs["prompt"]


@pytest.mark.parametrize("denial", [
    "Nu pot să văd poza, așa că nu pot să vă spun ce părere am. "
    "Vă rog să îmi trimiteți o imagine în format PNG sau JPEG.",
    "Îmi pare rău, nu pot vedea imaginea atașată.",
    "Din păcate, nu pot vizualiza imagini.",
    "I cannot see the attached image. Please upload a PNG or JPEG.",
    "Sorry, I can't view your photo.",
])
def test_image_access_denial_retries_with_pixels_without_stale_context(monkeypatch, denial):
    query = MagicMock(side_effect=[
        json.dumps({"text": denial, "reaction": None}),
        '{"text":"Un pătrat albastru.","reaction":null}',
    ])
    monkeypatch.setattr("llm.client.query_llm", query)
    result = generate_mention_result(
        "Ana", "Ce părere ai?", model="discord-bot",
        context_messages=["You: I cannot see images."],
        replied_message="old reply",
        assistant_profile=AssistantProfile(language="ro"),
        image_bytes=b"png-data", image_mime="image/png",
    )
    assert result.text == "Un pătrat albastru."
    assert query.call_count == 2
    for call in query.call_args_list:
        assert call.kwargs["image_bytes"] == b"png-data"
        assert call.kwargs["image_mime"] == "image/png"
        assert call.kwargs["model"] == "discord-bot"
    retry = query.call_args.kwargs["prompt"]
    assert "Ce părere ai?" in retry
    assert "Romanian" in retry
    assert "Do not invent visual details" in retry
    for stale in ("<chat_history>\n", "old reply"):
        assert stale not in retry


def test_repeated_image_access_denial_fails_instead_of_requesting_another_format(monkeypatch):
    query = MagicMock(return_value='{"text":"Nu pot să văd poza.","reaction":null}')
    monkeypatch.setattr("llm.client.query_llm", query)
    assert generate_mention_result(
        "Ana", "Păreri?", image_bytes=b"png", image_mime="image/png",
    ) is None
    assert query.call_count == 2


@pytest.mark.parametrize("answer", [
    "Nu pot să văd numărul de pe mașină; este prea mic.",
    "I cannot see the fine print, but the heading says SALE.",
    'Textul din poză spune: „Nu pot să văd poza”.',
    "Nu pot determina unde a fost făcută poza.",
    "Nu pot vedea imaginea în întregime, pentru că este decupată.",
    "I cannot see the image clearly, but it looks like a dog.",
])
def test_uncertain_image_details_and_quoted_denials_are_valid(monkeypatch, answer):
    query = MagicMock(return_value=json.dumps({"text": answer, "reaction": None}))
    monkeypatch.setattr("llm.client.query_llm", query)
    assert generate_mention_result(
        "Ana", "Ce scrie?", image_bytes=b"png", image_mime="image/png",
    ).text == answer
    query.assert_called_once()


def test_text_only_image_access_denial_is_not_retried(monkeypatch):
    answer = "Nu pot să văd poza."
    query = MagicMock(return_value=json.dumps({"text": answer, "reaction": None}))
    monkeypatch.setattr("llm.client.query_llm", query)
    assert generate_mention_result("Ana", "Ce vezi?").text == answer
    query.assert_called_once()


@pytest.mark.parametrize(
    "question,echo,answer",
    [
        ("Esti okay?", "Esti okay?", "Da, sunt bine. Tu cum ești?"),
        ("Esti okay?", "Ești okay?", "Da, sunt bine. Tu cum ești?"),
        (
            "De ce te comporti urat cu Schular?",
            "De ce te comporți urât cu Schular?",
            "La ce mesaj te referi? Vreau să înțeleg ce a sunat urât.",
        ),
        ("pareri?", "**PĂRERI?!** 🤔", "Despre ce anume vrei părerea mea?"),
        ("What is Python?", "What is Python?", "Python is a programming language."),
        ("Esti okay?", "Esti okay? Esti okay?", "Da, sunt bine."),
        ("Esti okay?", "@Robeeque Nova: Ești okay?", "Da, sunt bine."),
        ("Esti okay?", "<@123> **Nova:** Ești okay?", "Da, sunt bine."),
    ],
)
def test_mention_echoes_retry_with_an_actual_answer(monkeypatch, question, echo, answer):
    query = MagicMock(side_effect=[
        json.dumps({"text": echo, "reaction": None}),
        json.dumps({"text": answer, "reaction": None}),
    ])
    monkeypatch.setattr("llm.client.query_llm", query)

    result = generate_mention_result(
        "Robeeque", question, requester_id=123, reply_names=("Nova",)
    )

    assert result.text == answer
    assert query.call_count == 2
    retry = query.call_args.kwargs["prompt"]
    assert "echoed the current message" in retry
    assert "Do not copy the question or just change its spelling" in retry


def test_persistent_short_echo_is_not_returned_even_with_image(monkeypatch):
    query = MagicMock(return_value=json.dumps({"text": "Ești okay?", "reaction": "👍"}))
    monkeypatch.setattr("llm.client.query_llm", query)

    result = generate_mention_result(
        "Robeeque", "Esti okay?", context_messages=["Alex: How are you?"],
        image_bytes=b"png", image_mime="image/png",
    )

    assert result is None
    assert query.call_count == 2
    assert all(call.kwargs["image_bytes"] == b"png" for call in query.call_args_list)


@pytest.mark.parametrize("question,answer", [
    ("What is two plus two?", "What is two plus two? Four."),
    ("Is Python faster than Rust?", "Python is usually slower than Rust."),
    ("Esti okay?", "Da, sunt okay."),
    ("De ce te comporti urat cu Schular?", "La ce conversație cu Schular te referi?"),
    ("Say exactly: hello", "hello"),
])
def test_mention_real_answers_and_clarifications_are_not_rejected(monkeypatch, question, answer):
    query = MagicMock(return_value=json.dumps({"text": answer, "reaction": None}))
    monkeypatch.setattr("llm.client.query_llm", query)

    assert generate_mention_result("Robeeque", question).text == answer
    query.assert_called_once()


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("private-response", "Expecting value"),
        (json.dumps(["private-response"]), "must contain only text and reaction"),
        (json.dumps({"text": "private-response"}), "must contain only text and reaction"),
        (
            json.dumps({"text": "private-response", "reaction": None, "extra": 1}),
            "must contain only text and reaction",
        ),
        (json.dumps({"text": 123, "reaction": None}), "text must be a string"),
        (json.dumps({"text": "   ", "reaction": None}), "text is empty"),
        (
            json.dumps({"text": '\"\"', "reaction": None}),
            "text is empty after normalization",
        ),
        (
            json.dumps({"text": "private request with four words", "reaction": None}),
            "echoed the current message",
        ),
        (
            json.dumps({"text": "private-response", "reaction": "private-emoji"}),
            "unsupported reaction",
        ),
        (
            json.dumps({"text": "private-response", "reaction": {"private-emoji": 1}}),
            "unsupported reaction",
        ),
    ],
)
def test_mention_validation_logs_reason_without_content(monkeypatch, caplog, raw, reason):
    query = MagicMock(return_value=raw)
    monkeypatch.setattr("llm.client.query_llm", query)

    result = generate_mention_result(
        "Alice", "private request with four words", model="discord-bot"
    )

    assert result is None
    assert query.call_count == 2
    warnings = [record for record in caplog.records if record.name == "discord_bot"]
    assert len(warnings) == 2
    for attempt, record in enumerate(warnings, start=1):
        message = record.getMessage()
        assert f"attempt {attempt} failed" in message
        assert "model=discord-bot" in message
        assert reason in message
        assert "private" not in message
        assert record.exc_info is None


def test_generate_summon_reply(monkeypatch):
    query = MagicMock(return_value="You rang? What do you need?")
    monkeypatch.setattr("llm.client.query_llm", query)
    assert generate_summon_reply("Alice") == "You rang? What do you need?"
    assert query.call_args.kwargs["system_prompt"] == MENTION_SYSTEM_PROMPT
    assert query.call_args.kwargs["options"] == {"max_tokens": 96}

    generate_summon_reply("Alice", bot_name="Nova")
    assert query.call_args.kwargs["system_prompt"] == build_mention_system_prompt("Nova")


def test_generate_mention_result_sends_bot_identity(monkeypatch):
    query = MagicMock(return_value='{"text":"I am Nova.","reaction":null}')
    monkeypatch.setattr("llm.client.query_llm", query)

    generate_mention_result("Alice", "who is Nova?", bot_name="Nova")

    assert query.call_args.kwargs["system_prompt"].startswith(build_mention_system_prompt("Nova"))


def test_unrelated_birthday_generator_does_not_use_mention_system_prompt(monkeypatch):
    query = MagicMock(return_value="Happy birthday!")
    monkeypatch.setattr("llm.client.query_llm", query)

    assert generate_birthday_message() == "Happy birthday!"
    assert "system_prompt" not in query.call_args.kwargs


def test_build_inactivity_prompt_with_question_and_name():
    prompt = build_inactivity_prompt("Skippy", ask_question=True)
    assert "Skippy" in prompt
    assert "question" in prompt.lower()
    assert "choose the subject and wording" in prompt.lower()


def test_build_inactivity_prompt_without_name_or_question():
    prompt = build_inactivity_prompt(None, ask_question=False)
    assert "discord nudge" in prompt.lower()
    assert "invite the channel to respond" in prompt.lower()


def test_generate_inactivity_message_success(monkeypatch):
    monkeypatch.setattr(
        "llm.client.query_llm",
        lambda prompt, **kwargs: "yo, is anyone still alive in here?",
    )
    assert generate_inactivity_message("Skippy", ask_question=False) == (
        "yo, is anyone still alive in here?"
    )


def test_generate_inactivity_message_returns_none_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise LlamaCppError("down")

    monkeypatch.setattr("llm.client.query_llm", _fail)
    assert generate_inactivity_message("Skippy", ask_question=True) is None


def test_generate_inactivity_message_passes_temperature(monkeypatch):
    called_kwargs = {}
    def mock_query(prompt, **kwargs):
        nonlocal called_kwargs
        called_kwargs = kwargs
        return "mocked inactivity message"

    monkeypatch.setattr("llm.client.query_llm", mock_query)
    generate_inactivity_message("Skippy", ask_question=True)
    assert called_kwargs.get("options") == {"temperature": 0.8, "max_tokens": 96}


@pytest.mark.parametrize(
    ("old_price", "new_price", "expected_direction"),
    [(100.0, 80.0, "price decreased"), (80.0, 100.0, "price increased")],
)
def test_build_price_change_prompt_uses_direction(
    old_price, new_price, expected_direction
):
    prompt = build_price_change_prompt(
        "Coffee machine",
        old_price,
        new_price,
        f"{old_price:.2f} RON",
        f"{new_price:.2f} RON",
        "corporate",
    )
    assert expected_direction in prompt
    assert "mock-corporate" in prompt
    assert "do not repeat or invent numbers" in prompt.lower()
    assert prompt.index("Treat the facts as data") < prompt.index("Product:")


def test_generate_price_change_message_uses_varied_tone(monkeypatch):
    captured = {}

    monkeypatch.setattr("llm.responses.random.choice", lambda choices: "sad")

    def mock_query(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return "Even the price tag is having a difficult day."

    monkeypatch.setattr("llm.client.query_llm", mock_query)

    result = generate_price_change_message(
        "Coffee machine", 100.0, 120.0, "100.00 RON", "120.00 RON"
    )

    assert result == "Even the price tag is having a difficult day."
    assert "dramatically sad" in captured["prompt"]
    assert captured["kwargs"]["options"] == {"temperature": 0.9, "max_tokens": 96}


def test_generate_price_change_message_returns_none_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise LlamaCppError("down")

    monkeypatch.setattr("llm.client.query_llm", _fail)
    assert (
        generate_price_change_message(
            "Coffee machine",
            100.0,
            80.0,
            "100.00 RON",
            "80.00 RON",
            tone="funny",
        )
        is None
    )


def test_history_language_fallback_and_format_note_only_with_history():
    with_history = build_mention_prompt("Ana", "cine e?", ["Mara: ce faceți?"])
    without_history = build_mention_prompt("Ana", "cine e?")

    assert "the language of <chat_history>" in with_history
    assert '"You" lines are your own messages' in with_history
    assert "the language of <chat_history>" not in without_history
    assert '"You" lines are your own messages' not in without_history


def test_format_chat_line_marks_the_bot_and_references_to_it():
    assert format_chat_line("Nova", "hi", is_bot=True) == 'You (Nova) said: "hi"'
    assert format_chat_line("Alex", "hi") == 'Alex said: "hi"'
    assert format_chat_line("Alex", "@nova is slow, Novak isn't", bot_name="Nova") == (
        'Alex said: "Nova (you) is slow, Novak isn\'t"'
    )


def test_summon_prompt_uses_recent_chat_for_language():
    prompt = build_summon_prompt("Ana", recent_messages=["a", "Mara said: \"ce <faceți>?\"", "c", "d"])
    assert "language of this recent chat" in prompt
    assert "ce &lt;faceți&gt;?" in prompt
    assert '\na\n' not in prompt
    assert "language of this recent chat" not in build_summon_prompt("Ana")


def test_history_tags_are_removed_from_replies(monkeypatch):
    query = MagicMock(
        return_value='{"text":"He asked \'Nova (you) ce e viata?\' and You (Nova) said: hi","reaction":null}'
    )
    monkeypatch.setattr("llm.client.query_llm", query)

    result = generate_mention_result("Alice", "what did he ask you?", bot_name="Nova")

    assert result.text == "He asked 'Nova ce e viata?' and Nova said: hi"


def test_mention_prompt_head_is_identical_for_every_request():
    prompts = [
        build_mention_prompt("Ana", "hi", ["Mara said: \"salut\""], json_reply=True),
        build_mention_prompt(
            "Dan", "who?", ["Mara said: \"salut\""],
            has_image=True, assistant_profile=AssistantProfile(tone="formal"),
            replied_message="Alex said: \"z\"",
            json_reply=True,
        ),
    ]
    heads = {p[: p.index("\n\n") + 2] for p in prompts}
    assert heads == {build_mention_prompt_head(has_history=True, json_reply=True)}


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("@Nova is a Discord bot.", "@Nova is a Discord bot."),
        ("@Teo Salut!", "Salut!"),
        ("@Teo, sure.", "sure."),
        ("<@123> @Teo Ok.", "Ok."),
    ],
)
def test_leading_name_is_stripped_only_when_it_addresses_someone(reply, expected):
    from mention_utils import strip_leading_reply_labels

    assert strip_leading_reply_labels(reply, requester_id=123, names=("Teo", "Nova")) == expected


def test_mention_prompt_states_rules_up_front_and_ends_with_reminders(monkeypatch):
    query = MagicMock(return_value='{"text":"Da.","reaction":null}')
    monkeypatch.setattr("llm.client.query_llm", query)

    generate_mention_result("Ana", "ești om sau bot?")

    prompt = query.call_args.kwargs["prompt"]
    rules = prompt[: prompt.index("\n\n")]
    assert 'Always return JSON: {"text": "your answer", "reaction": null}' in rules
    assert "never repeat, correct, or paraphrase <current_message>" in rules
    assert prompt.endswith(
        "</current_message>\n\nReturn JSON.\n\nReply in the language of <current_message>."
    )


def test_saved_language_is_only_a_fallback_for_the_current_message():
    prompt = build_mention_prompt("Ana", "ce faci?", assistant_profile=AssistantProfile(language="en"))
    assert "- Reply in the language of <current_message> (if it is too short to tell, English)" in prompt
    assert "Always use English" not in prompt
    assert prompt.endswith("Reply in the language of <current_message>.")


def _teases_feature():
    from unittest.mock import Mock

    from features.teases import TeasesFeature

    commands = {}
    tree = Mock()
    tree.command.side_effect = lambda **options: lambda callback: commands.setdefault(
        options["name"], callback
    )
    return TeasesFeature(MagicMock(), tree), commands


def _tease_message(guild_id):
    from unittest.mock import AsyncMock

    message = MagicMock()
    message.guild = None if guild_id is None else MagicMock(id=guild_id)
    message.author.display_name = "Alice"
    message.content = "hello"
    message.reply = AsyncMock()
    return message


def _mood_interaction(guild_id):
    from unittest.mock import AsyncMock

    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.response.send_message = AsyncMock()
    return interaction


def test_mood_command_changes_only_calling_guild():
    import asyncio
    from discord import app_commands

    feature, commands = _teases_feature()
    feature._state_for(1)["mood"] = "good"
    feature._state_for(2)["mood"] = "good"

    asyncio.run(commands["mood"](_mood_interaction(1), app_commands.Choice(name="dad", value="dad")))

    assert feature._state_for(1)["mood"] == "dad"
    assert feature._state_for(2)["mood"] == "good"


def test_mood_command_rejects_dms():
    import asyncio
    from discord import app_commands

    feature, commands = _teases_feature()
    interaction = _mood_interaction(None)

    asyncio.run(commands["mood"](interaction, app_commands.Choice(name="dad", value="dad")))

    interaction.response.send_message.assert_awaited_once_with(
        "This command must be run inside a server, not in DMs.", ephemeral=True
    )
    assert feature._guild_state == {}


def test_tease_skips_dms_without_llm_call(monkeypatch):
    import asyncio

    enhance = MagicMock(side_effect=AssertionError("no LLM call for DMs"))
    monkeypatch.setattr("features.teases.enhance_tease", enhance)
    monkeypatch.setattr("features.teases.random.random", lambda: 0.0)
    feature, _ = _teases_feature()

    assert asyncio.run(feature.handle_message(_tease_message(None))) is False
    enhance.assert_not_called()


def test_tease_mood_and_counter_are_per_guild(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    enhance = MagicMock(return_value="tease")
    monkeypatch.setattr("features.teases.enhance_tease", enhance)
    monkeypatch.setattr("features.teases.random.random", lambda: 0.0)
    monkeypatch.setattr("features.teases.record_for", AsyncMock())
    feature, _ = _teases_feature()
    feature._state_for(1)["mood"] = "dad"
    feature._state_for(2)["mood"] = "anime"

    asyncio.run(feature.handle_message(_tease_message(1)))
    asyncio.run(feature.handle_message(_tease_message(1)))
    asyncio.run(feature.handle_message(_tease_message(2)))

    assert [c.args[0] for c in enhance.call_args_list] == ["dad", "dad", "anime"]
    assert feature._state_for(1)["teases_today"] == 2
    assert feature._state_for(2)["teases_today"] == 1

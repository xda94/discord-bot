import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import db
from features.llm_feedback import LLMFeedbackFeature, THUMBS_DOWN, THUMBS_UP


def test_feedback_reactions_are_not_seeded_and_only_requester_can_rate(tmp_db):
    feature = object.__new__(LLMFeedbackFeature)
    feature.bot_id = 999
    message = MagicMock()
    message.id = 12345
    message.guild = SimpleNamespace(id=444)
    message.add_reaction = AsyncMock()

    asyncio.run(
        feature.register_reply(
            message,
            requester_user_id=77,
            category="mention",
            model="discord-bot",
            prompt_version="mention-v1",
        )
    )

    message.add_reaction.assert_not_awaited()

    asyncio.run(
        feature.handle_raw_reaction_add(
            SimpleNamespace(user_id=999, message_id=12345, emoji=THUMBS_UP)
        )
    )
    assert db.get_llm_response_feedback(12345)[2] is None

    asyncio.run(
        feature.handle_raw_reaction_add(
            SimpleNamespace(user_id=88, message_id=12345, emoji=THUMBS_UP)
        )
    )
    assert db.get_llm_response_feedback(12345)[2] is None

    asyncio.run(
        feature.handle_raw_reaction_add(
            SimpleNamespace(user_id=77, message_id=12345, emoji=THUMBS_DOWN)
        )
    )
    assert db.get_llm_response_feedback(12345)[2] == -1
    assert db.get_llm_response_feedback(12345)[3:6] == (
        444,
        "discord-bot",
        "mention-v1",
    )


def test_feedback_summary_is_scoped_to_one_guild(tmp_db):
    db.track_llm_response(
        1, 10, "mention", guild_id=100, model="model-a", prompt_version="v1"
    )
    db.track_llm_response(
        2, 11, "mention", guild_id=100, model="model-a", prompt_version="v1"
    )
    db.track_llm_response(
        3, 12, "mention", guild_id=200, model="model-b", prompt_version="v1"
    )
    assert db.set_llm_response_rating(1, 10, 1)
    assert db.set_llm_response_rating(2, 11, -1)
    assert db.set_llm_response_rating(3, 12, 1)

    assert db.get_llm_feedback_summary(100) == [
        ("mention", "model-a", "v1", 2, 1, 1)
    ]

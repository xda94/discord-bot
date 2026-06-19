from unittest.mock import MagicMock

from features.inactivity import pick_chatter


def _msg(author_id: int, *, is_bot: bool = False):
    msg = MagicMock()
    msg.author.id = author_id
    msg.author.bot = is_bot
    return msg


def test_pick_chatter_returns_a_human():
    result = pick_chatter([_msg(1), _msg(2), _msg(1)])
    assert result.id in {1, 2}
    assert result.bot is False


def test_pick_chatter_excludes_bots():
    result = pick_chatter([_msg(99, is_bot=True), _msg(5)])
    assert result.id == 5


def test_pick_chatter_none_when_only_bots():
    assert pick_chatter([_msg(98, is_bot=True), _msg(99, is_bot=True)]) is None


def test_pick_chatter_none_when_empty():
    assert pick_chatter([]) is None

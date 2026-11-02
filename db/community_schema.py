from __future__ import annotations

COMMUNITY_TABLES = ("community_votes", "community_titles", "bingo_marks", "museum_quotes", "bingo_boards", "bingo_habits", "community_sessions")


def migrate(c):
    for table in COMMUNITY_TABLES:
        c.execute(f"DROP TABLE IF EXISTS {table}")

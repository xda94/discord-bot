"""Mention-only natural commands with immediate, privacy-preserving delivery."""

from __future__ import annotations

import time

import discord

from features.flights import format_user_flight_trackers
from mention_utils import extract_mention_text
from natural_commands import (
    ClearWishlistTargetAction,
    DeleteWishlistAction,
    NaturalCommandError,
    RefreshWishlistAction,
    ReminderAction,
    ShowFlightsAction,
    ShowWishlistAction,
    TrackURLAction,
    WishlistGraphAction,
    WishlistRestockAction,
    WishlistTargetAction,
    parse_natural_command,
)


class NaturalCommandsFeature:
    def __init__(self, client, *, bot_id: int, wishlist, flights, reminders):
        self.client = client
        self.bot_id = bot_id
        self.wishlist = wishlist
        self.flights = flights
        self.reminders = reminders

    async def _acknowledge(self, message) -> None:
        """Best-effort success reaction; never rerun the durable action."""
        try:
            await message.add_reaction("✅")
        except Exception:
            pass

    async def _reply_error(self, message, text: str) -> None:
        await message.reply(
            text,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )

    async def _send_private(self, message, chunks) -> bool:
        """Send all result chunks to the author, never to the source channel."""
        delivered = True
        for chunk in chunks:
            try:
                await message.author.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )
            except Exception:
                delivered = False
        return delivered

    async def _track(self, user_id: int, url: str):
        """Return the add status without exposing scraped item details."""
        result = await self.wishlist.add_item_for_user(user_id, url)
        return result.status

    async def handle_message(self, message: discord.Message) -> bool:
        text = extract_mention_text(message, self.bot_id)
        if text is None:
            return False
        replied_text = ""
        resolved = getattr(getattr(message, "reference", None), "resolved", None)
        if resolved is not None:
            replied_text = (
                getattr(resolved, "clean_content", None)
                or getattr(resolved, "content", "")
            )
        parsed = parse_natural_command(text, replied_text=replied_text)
        if parsed is None:
            return False
        if isinstance(parsed, NaturalCommandError):
            await self._reply_error(message, parsed.message)
            return True

        author_id = message.author.id
        try:
            if isinstance(parsed, ReminderAction):
                remind_at = time.time() + parsed.seconds
                reminder_id = self.reminders.create_reminder_at(
                    author_id, message.channel.id, remind_at, parsed.text
                )
                if reminder_id is None:
                    await self._reply_error(
                        message, "I couldn't save that reminder. Please try again."
                    )
                    return True
                await self._acknowledge(message)
                return True

            if isinstance(parsed, TrackURLAction):
                status = await self._track(author_id, parsed.url)
                if status == "added":
                    await self._acknowledge(message)
                else:
                    errors = {
                        "exists": "That URL is already in your tracking list.",
                        "blocked": "The source blocked or timed out, so the URL was not added.",
                        "unsupported": "The page had no supported price or stock data, so the URL was not added.",
                        "database-error": "The page was read, but the database could not save it. Please try again.",
                        "invalid": "That is not a valid HTTP(S) URL.",
                    }
                    await self._reply_error(message, errors.get(status, "The URL could not be added."))
                return True

            if isinstance(parsed, ShowFlightsAction):
                delivered = await self._send_private(
                    message, format_user_flight_trackers(author_id)
                )
                if delivered:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(
                        message,
                        "I couldn't send the private result. Enable DMs or use the corresponding slash command.",
                    )
                return True

            if isinstance(parsed, ShowWishlistAction):
                chunks = self.wishlist.format_items_for_user(author_id, parsed.currency)
                delivered = await self._send_private(message, chunks)
                if delivered:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(
                        message,
                        "I couldn't send the private result. Enable DMs or use the corresponding slash command.",
                    )
                return True

            if isinstance(parsed, DeleteWishlistAction):
                if self.wishlist.delete_item_for_user(author_id, parsed.url):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                return True

            if isinstance(parsed, WishlistTargetAction):
                if self.wishlist.set_target_for_user(
                    author_id, parsed.url, parsed.price, parsed.currency
                ):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                return True

            if isinstance(parsed, ClearWishlistTargetAction):
                if self.wishlist.clear_target_for_user(author_id, parsed.url):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                return True

            if isinstance(parsed, WishlistRestockAction):
                if self.wishlist.set_restock_only_for_user(
                    author_id, parsed.url, parsed.enabled
                ):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                return True

            if isinstance(parsed, RefreshWishlistAction):
                chunks = await self.wishlist.refresh_items_for_user(author_id, parsed.url)
                delivered = await self._send_private(message, chunks)
                if delivered:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(
                        message,
                        "The refresh was attempted, but I couldn't deliver its private result. Enable DMs or use the corresponding slash command.",
                    )
                return True

            if isinstance(parsed, WishlistGraphAction):
                await self.wishlist.send_graph_for_user(
                    message.author,
                    url=parsed.url,
                    currency=parsed.currency,
                    days=parsed.days,
                    percentage=parsed.percentage,
                )
                await self._acknowledge(message)
                return True

        except Exception:
            # A recognized command is consumed even when its helper or private
            # transport fails, so the ordinary LLM cannot duplicate the action.
            await self._reply_error(
                message,
                "I couldn't complete that action. Enable DMs or use the corresponding slash command.",
            )
            return True

        return True

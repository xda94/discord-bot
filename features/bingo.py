import asyncio
from typing import Optional

import discord
from discord import app_commands

from analytics import record
from db import community
from features.humor_common import interaction_language, local_day, safe_text, text, week_key


STRINGS = {
    "human": ("Only human members of this server can use bingo.", "Doar membrii umani ai acestui server pot folosi bingo."),
    "board": ("Weekly community bingo · {week}", "Bingo săptămânal al comunității · {week}"),
    "guide": ("Choose a numbered cell, mark it, then ask another human member to run /bingo and confirm it. Only confirmed cells count. Weeks start Monday using the bot's local calendar. No automatic observation or weekly posts.", "Alege o celulă numerotată, marcheaz-o, apoi roagă alt membru uman să folosească /bingo și să o confirme. Contează doar celulele confirmate. Săptămâna începe luni, după calendarul local al botului. Fără observații automate sau postări săptămânale."),
    "open": ("Unmarked", "Nemarcată"),
    "pending": ("Pending confirmation", "Așteaptă confirmarea"),
    "confirmed": ("Confirmed", "Confirmată"),
    "lines": ("Completed rows: {rows} · columns: {columns} · diagonals: {diagonals}", "Linii complete: {rows} · coloane: {columns} · diagonale: {diagonals}"),
    "selected": ("Selected cell: {cell}", "Celula selectată: {cell}"),
    "choose": ("Choose a cell", "Alege o celulă"),
    "mark": ("Mark cell", "Marchează celula"),
    "confirm": ("Confirm another member's mark", "Confirmă marcajul altui membru"),
    "marked": ("Marked. Another human member must confirm it.", "Am marcat. Alt membru uman trebuie să confirme."),
    "confirmation": ("Confirmed. This cell now counts toward bingo.", "Am confirmat. Această celulă contează acum la bingo."),
    "unchanged": ("This cell is already marked or confirmed; its first marker and confirmation stay unchanged.", "Această celulă este deja marcată sau confirmată; primul marcaj și confirmarea rămân neschimbate."),
    "unmarked": ("Mark this cell before another member confirms it.", "Marchează această celulă înainte ca alt membru să o confirme."),
    "self_confirm": ("Another human member must confirm your mark.", "Alt membru uman trebuie să confirme marcajul tău."),
    "invalid_cell": ("Choose a cell from 1 to 9.", "Alege o celulă de la 1 la 9."),
    "permission": ("Managing bingo habits requires Manage Server permission.", "Administrarea obiceiurilor de bingo cere permisiunea Gestionează serverul."),
    "invalid_habit": ("Provide both translations, each 1–200 characters, and a positive ID when editing.", "Scrie ambele traduceri, fiecare de 1–200 de caractere, și un ID pozitiv pentru editare."),
    "habit_saved": ("Habit ID {id} saved. Changes apply to future boards; this week's snapshot stays unchanged.", "Am salvat obiceiul cu ID {id}. Modificările se aplică tablelor viitoare; tabla săptămânii curente rămâne neschimbată."),
    "habit_deleted": ("Habit ID {id} removed from future boards; existing snapshots stay unchanged.", "Am eliminat obiceiul cu ID {id} din tablele viitoare; tablele existente rămân neschimbate."),
    "habits": ("Active habits ({count}/50). Use these IDs with /bingo-obicei or /bingo-obicei-sterge:", "Obiceiuri active ({count}/50). Folosește aceste ID-uri cu /bingo-obicei sau /bingo-obicei-sterge:"),
    "minimum": ("Keep at least nine active habits.", "Păstrează cel puțin nouă obiceiuri active."),
    "limit": ("The pool already has fifty active habits. Edit or remove an existing habit first.", "Lista are deja cincizeci de obiceiuri active. Editează sau elimină întâi un obicei existent."),
    "missing_habit": ("No active habit with that ID exists in this server.", "Nu există un obicei activ cu acest ID pe server."),
}


def _t(key, language, **values):
    return STRINGS[key][language == "ro"].format(**values)


def _current_week():
    return week_key(local_day())


def _positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 9223372036854775807


class BingoView(discord.ui.View):
    def __init__(self, feature, board, owner_id, channel_id, language, selected=0):
        super().__init__(timeout=600)
        self.selected = selected
        active = board["week_key"] == _current_week()
        marks = {mark["cell_index"]: mark for mark in board["marks"]}
        options = []
        for index, cell in enumerate(board["cells"]):
            mark = marks.get(index)
            status = "open" if not mark else "pending" if mark["confirmer_id"] is None else "confirmed"
            options.append(discord.SelectOption(label=f"{index + 1}. {safe_text(cell['text_' + language], 94)}"[:100], value=str(index), description=_t(status, language), default=index == selected))
        selector = discord.ui.Select(placeholder=_t("choose", language), options=options, custom_id=f"bingo:{board['id']}:select", disabled=not active)

        async def select_callback(interaction):
            value = selector.values[0] if selector.values else ""
            index = int(value) if value in {str(index) for index in range(9)} else None
            await feature.handle_control(interaction, board["guild_id"], board["id"], owner_id, channel_id, "select", index)

        selector.callback = select_callback
        self.add_item(selector)
        for action in ("mark", "confirm"):
            button = discord.ui.Button(label=_t(action, language), custom_id=f"bingo:{board['id']}:{action}", disabled=not active, row=1)

            async def callback(interaction, selected_action=action):
                await feature.handle_control(interaction, board["guild_id"], board["id"], owner_id, channel_id, selected_action, self.selected)

            button.callback = callback
            self.add_item(button)


class BingoFeature:
    def __init__(self, client, tree):
        self.client = client
        self.tree = tree
        self._register_commands()

    async def _defer(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _reply(self, interaction, content, **kwargs):
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True, **kwargs)

    async def _record(self, category, action, guild_id):
        await record(category, "bingo/" + action, guild_id=guild_id, scope_type="guild")

    async def _admit(self, interaction, language):
        if interaction.guild_id is None:
            await self._reply(interaction, text("guild_only", language))
            return False
        if not _positive_id(interaction.user.id) or getattr(interaction.user, "bot", True) or getattr(interaction.guild, "id", None) != interaction.guild_id or getattr(getattr(interaction.user, "guild", None), "id", None) != interaction.guild_id or getattr(interaction.channel, "id", None) != interaction.channel_id or getattr(getattr(interaction.channel, "guild", None), "id", None) != interaction.guild_id:
            await self._reply(interaction, _t("human", language))
            return False
        return True

    async def _manage(self, interaction, language):
        if not await self._admit(interaction, language):
            return False
        if not getattr(getattr(interaction.user, "guild_permissions", None), "manage_guild", False):
            await self._reply(interaction, _t("permission", language))
            return False
        return True

    def _render_board(self, board, language, selected=0):
        marks = {mark["cell_index"]: mark for mark in board["marks"]}
        confirmed = {index for index, mark in marks.items() if mark["confirmer_id"] is not None}
        rows = sum(all(index in confirmed for index in range(start, start + 3)) for start in (0, 3, 6))
        columns = sum(all(index in confirmed for index in (start, start + 3, start + 6)) for start in range(3))
        diagonals = sum(all(index in confirmed for index in line) for line in ((0, 4, 8), (2, 4, 6)))
        embed = discord.Embed(title=_t("board", language, week=board["week_key"]), description=_t("guide", language))
        for index, cell in enumerate(board["cells"]):
            mark = marks.get(index)
            status = "open" if not mark else "pending" if mark["confirmer_id"] is None else "confirmed"
            symbol = "⬜" if not mark else "🟨" if status == "pending" else "✅"
            embed.add_field(name=f"{index + 1}. {symbol} {_t(status, language)}", value=safe_text(cell["text_" + language], 450), inline=True)
        embed.set_footer(text=_t("lines", language, rows=rows, columns=columns, diagonals=diagonals) + "\n" + _t("selected", language, cell=selected + 1))
        return embed

    async def bingo(self, interaction):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        try:
            board = await asyncio.to_thread(community.get_or_create_board, interaction.guild_id, week_key=_current_week())
            await self._reply(interaction, None, embed=self._render_board(board, language), view=BingoView(self, board, interaction.user.id, interaction.channel_id, language))
            return board
        except Exception:
            await self._record("failure", "show", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def handle_control(self, interaction, guild_id, board_id, owner_id, channel_id, action, cell_index):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        if interaction.user.id != owner_id:
            await self._reply(interaction, text("not_owner", language))
            return None
        if interaction.guild_id != guild_id or interaction.channel_id != channel_id:
            await self._reply(interaction, text("expired", language))
            return None
        if isinstance(cell_index, bool) or not isinstance(cell_index, int) or not 0 <= cell_index <= 8 or action not in ("select", "mark", "confirm"):
            await self._reply(interaction, _t("invalid_cell", language))
            return None
        try:
            board = await asyncio.to_thread(community.get_board, guild_id, board_id)
            if not board or board["week_key"] != _current_week():
                await self._reply(interaction, text("expired", language))
                return None
            status = "selected"
            if action != "select":
                mutate = community.mark_bingo_cell if action == "mark" else community.confirm_bingo_cell
                result = await asyncio.to_thread(mutate, guild_id, board_id, cell_index, interaction.user.id)
                status, board = result["status"], result["board"]
                if status in ("missing", "closed"):
                    await self._reply(interaction, text("expired", language))
                    return None
                if status in ("marked", "confirmed"):
                    await self._record("control", action, guild_id)
            await interaction.edit_original_response(embed=self._render_board(board, language, cell_index), view=BingoView(self, board, owner_id, channel_id, language, cell_index), allowed_mentions=discord.AllowedMentions.none())
            key = {"confirmed": "confirmation", "forbidden": "self_confirm"}.get(status, status)
            await self._reply(interaction, _t(key, language, cell=cell_index + 1))
            return board
        except Exception:
            await self._record("failure", "control", guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def _habit_list(self, interaction, language, notice):
        habits = await asyncio.to_thread(community.list_habits, interaction.guild_id)
        content = notice + "\n" + _t("habits", language, count=len(habits))
        for habit in habits:
            line = f"\n#{habit['id']}: {safe_text(habit['text_' + language], 200)}"
            if len(content) + len(line) > 1900:
                await self._reply(interaction, content)
                content = _t("habits", language, count=len(habits))
            content += line
        await self._reply(interaction, content)

    async def save_habit(self, interaction, text_ro, text_en, habit_id=None):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._manage(interaction, language):
            return None
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in (text_ro, text_en)) or habit_id is not None and not _positive_id(habit_id):
            await self._reply(interaction, _t("invalid_habit", language))
            return None
        try:
            result = await asyncio.to_thread(community.save_habit, interaction.guild_id, text_en, text_ro, habit_id=habit_id)
            if result["status"] == "saved":
                await self._habit_list(interaction, language, _t("habit_saved", language, id=result["habit"]["id"]))
            else:
                await self._reply(interaction, _t("limit" if result["status"] == "limit" else "missing_habit", language))
            return result
        except Exception:
            await self._record("failure", "habit-save", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def delete_habit(self, interaction, habit_id):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._manage(interaction, language):
            return None
        if not _positive_id(habit_id):
            await self._reply(interaction, _t("invalid_habit", language))
            return None
        try:
            result = await asyncio.to_thread(community.delete_habit, interaction.guild_id, habit_id)
            if result["status"] == "deleted":
                await self._habit_list(interaction, language, _t("habit_deleted", language, id=habit_id))
            else:
                await self._reply(interaction, _t("minimum" if result["status"] == "minimum" else "missing_habit", language))
            return result
        except Exception:
            await self._record("failure", "habit-delete", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    def _register_commands(self):
        @self.tree.command(name="bingo", description="Bingo săptămânal cu confirmare umană / Weekly bingo with human confirmation")
        @app_commands.guild_only()
        async def bingo(interaction: discord.Interaction):
            await self.bingo(interaction)

        @self.tree.command(name="bingo-obicei", description="Adaugă sau editează obiceiuri bilingve / Add or edit bilingual habits")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.rename(text_en="text-en")
        @app_commands.describe(text="Text românesc, 1–200 caractere / Romanian text, 1–200 characters", text_en="Text englezesc, 1–200 caractere / English text, 1–200 characters", id="ID pentru editare; omite pentru adăugare / Edit ID; omit to add")
        async def bingo_obicei(interaction: discord.Interaction, text: str, text_en: str, id: Optional[int] = None):
            await self.save_habit(interaction, text, text_en, id)

        @self.tree.command(name="bingo-obicei-sterge", description="Șterge un obicei; păstrează minimum nouă / Remove a habit; keep at least nine")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(id="ID obicei de eliminat / Habit ID to remove")
        async def bingo_obicei_sterge(interaction: discord.Interaction, id: int):
            await self.delete_habit(interaction, id)

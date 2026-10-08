"""เช็คชื่อทั่วไป (/daily_checkin): แอดมินหรือรีเซปชั่นตั้งหัวข้อเอง เช่น ประชุม อีเวนต์ ซ้อม แล้วให้คนกดตอบ

ไม่เกี่ยวกับการมาทำงาน (เข้างานใช้ปุ่มใน /panel_staff) — กระดานแต่ละอันแยกกันตามข้อความ
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_MAIN
from core.utils import NOT_RECEPTION, discord_ts, from_iso, is_reception, now_utc, to_iso

log = logging.getLogger("olp.dailycheck")

SCHEMA = """
CREATE TABLE IF NOT EXISTS roll_call_boards (
    message_id INTEGER PRIMARY KEY,
    topic      TEXT    NOT NULL,
    role_id    INTEGER,
    created_by INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS roll_call_answers (
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    status     TEXT    NOT NULL,   -- YES | NO | MAYBE
    checked_at TEXT    NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
"""

ANSWERS = {
    "YES": ("✅", "มา"),
    "NO": ("❌", "ไม่มา"),
    "MAYBE": ("🤔", "ยังไม่แน่ใจ"),
}


class RollCallView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="มา", emoji="✅", style=discord.ButtonStyle.success, custom_id="olp:daily:in")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").answer(interaction, "YES")

    @discord.ui.button(label="ไม่มา", emoji="❌", style=discord.ButtonStyle.secondary, custom_id="olp:daily:off")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").answer(interaction, "NO")

    @discord.ui.button(label="ยังไม่แน่ใจ", emoji="🤔", style=discord.ButtonStyle.secondary, custom_id="olp:daily:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").answer(interaction, "MAYBE")


class DailyCheckCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def cog_load(self) -> None:
        assert self.db.conn is not None
        await self.db.conn.executescript(SCHEMA)
        await self.db.conn.commit()

    async def board_embed(self, guild: discord.Guild | None, board: dict) -> discord.Embed:
        rows = await self.db.fetchall(
            "SELECT * FROM roll_call_answers WHERE message_id = ? ORDER BY checked_at", (board["message_id"],)
        )
        embed = discord.Embed(
            title=f"📋 เช็คชื่อ · {board['topic']}",
            description=f"กดตอบด้านล่าง เปลี่ยนคำตอบได้ตลอด\nตั้งโดย <@{board['created_by']}>",
            color=COLOR_MAIN,
        )
        for status, (emoji, label) in ANSWERS.items():
            group = [r for r in rows if r["status"] == status]
            names = "\n".join(f"<@{r['user_id']}> · {discord_ts(from_iso(r['checked_at']))}" for r in group)
            embed.add_field(name=f"{emoji} {label} ({len(group)})", value=names[:1024] or "-", inline=True)

        role = guild.get_role(board["role_id"]) if guild and board.get("role_id") else None
        if role is not None:
            answered = {r["user_id"] for r in rows}
            waiting = [m for m in role.members if not m.bot and m.id not in answered]
            if waiting:
                embed.add_field(
                    name=f"⏳ ยังไม่ตอบ ({len(waiting)})",
                    value=", ".join(m.mention for m in waiting)[:1024],
                    inline=False,
                )
        return embed

    async def answer(self, interaction: discord.Interaction, status: str) -> None:
        board = await self.db.fetchone(
            "SELECT * FROM roll_call_boards WHERE message_id = ?", (interaction.message.id,)
        )
        if board is None:
            await interaction.response.send_message("กระดานนี้ปิดไปแล้วค่ะ", ephemeral=True)
            return
        if board.get("role_id") and isinstance(interaction.user, discord.Member):
            if not any(r.id == board["role_id"] for r in interaction.user.roles):
                await interaction.response.send_message(f"เช็คชื่อนี้สำหรับ <@&{board['role_id']}> ค่ะ", ephemeral=True)
                return

        await self.db.execute(
            "INSERT INTO roll_call_answers (message_id, user_id, status, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET status = excluded.status, checked_at = excluded.checked_at",
            (board["message_id"], interaction.user.id, status, to_iso(now_utc())),
        )
        await interaction.response.edit_message(embed=await self.board_embed(interaction.guild, board))
        emoji, label = ANSWERS[status]
        await interaction.followup.send(f"{emoji} ตอบว่า **{label}** แล้ว", ephemeral=True)

    @app_commands.command(name="daily_checkin", description="โพสต์กระดานเช็คชื่อ (ประชุม / อีเวนต์ / นัดหมาย) — แอดมินหรือรีเซปชั่น")
    @app_commands.describe(
        topic="หัวข้อ เช่น ประชุมทีม ศุกร์ 21:00",
        role="ให้เฉพาะ Role นี้ตอบ และแสดงรายชื่อคนที่ยังไม่ตอบ (ไม่ใส่ = ทุกคนตอบได้)",
    )
    async def daily_checkin(
        self, interaction: discord.Interaction, topic: str, role: discord.Role | None = None
    ) -> None:
        if not is_reception(interaction.user, self.cfg):
            await interaction.response.send_message(NOT_RECEPTION, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        board = {"topic": topic[:200], "role_id": role.id if role else None, "created_by": interaction.user.id}
        message = await interaction.channel.send(
            embed=discord.Embed(title=f"📋 เช็คชื่อ · {board['topic']}", color=COLOR_MAIN), view=RollCallView()
        )
        board["message_id"] = message.id
        await self.db.execute(
            "INSERT INTO roll_call_boards (message_id, topic, role_id, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (message.id, board["topic"], board["role_id"], board["created_by"], to_iso(now_utc())),
        )
        await message.edit(embed=await self.board_embed(interaction.guild, board))
        await interaction.followup.send("โพสต์กระดานเช็คชื่อแล้วค่ะ", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(RollCallView())
    await bot.add_cog(DailyCheckCog(bot))

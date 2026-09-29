"""เช็คชื่อพนักงานรายวัน: บอทโพสต์กระดานเช็คชื่อในห้อง staff-chat ทุกวัน ให้พนักงานกด ✅ มา / 🛌 หยุด

แยกจากระบบเข้า/ออกงาน (ไม่นับชั่วโมง) — ใช้ยืนยันว่าวันนี้ใครมาทำงาน แม้จะซ่อนสถานะออนไลน์ไว้
"""
from __future__ import annotations

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.embeds import COLOR_MAIN
from core.utils import discord_ts, from_iso, is_admin, now_utc, to_iso

log = logging.getLogger("olp.dailycheck")

STATUS_IN = "IN"
STATUS_OFF = "OFF"


class DailyCheckView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="มาทำงานวันนี้", emoji="✅", style=discord.ButtonStyle.success, custom_id="olp:daily:in")
    async def check_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").mark(interaction, STATUS_IN)

    @discord.ui.button(label="หยุดวันนี้", emoji="🛌", style=discord.ButtonStyle.secondary, custom_id="olp:daily:off")
    async def day_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").mark(interaction, STATUS_OFF)


class DailyCheckCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def cog_load(self) -> None:
        self.auto_post.start()

    async def cog_unload(self) -> None:
        self.auto_post.cancel()

    # ------------------------------------------------------------ ข้อมูล
    def _day_of(self, message: discord.Message) -> str:
        """วันที่ของกระดาน = วันที่ (เวลาไทย) ที่โพสต์ข้อความนั้น"""
        return message.created_at.astimezone(self.cfg.tz).date().isoformat()

    async def _rows(self, day: str) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM daily_checkin WHERE day = ? ORDER BY checked_at", (day,)
        )

    def _staff_members(self, guild: discord.Guild | None) -> list[discord.Member]:
        if guild is None:
            return []
        members: dict[int, discord.Member] = {}
        for role_id in self.cfg.staff_role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                members.update({m.id: m for m in role.members if not m.bot})
        return sorted(members.values(), key=lambda m: m.display_name.lower())

    async def board_embed(self, guild: discord.Guild | None, day: str) -> discord.Embed:
        rows = await self._rows(day)
        came = [r for r in rows if r["status"] == STATUS_IN]
        off = [r for r in rows if r["status"] == STATUS_OFF]
        answered = {r["user_id"] for r in rows}
        waiting = [m for m in self._staff_members(guild) if m.id not in answered]

        def lines(items: list[dict]) -> str:
            text = "\n".join(f"<@{r['user_id']}> · {discord_ts(from_iso(r['checked_at']))}" for r in items)
            return text[:1024] or "-"

        date_text = dt.date.fromisoformat(day).strftime("%d/%m/%Y")
        embed = discord.Embed(
            title=f"📋 เช็คชื่อพนักงาน · {date_text}",
            description="กดปุ่มด้านล่างเพื่อเช็คชื่อวันนี้ได้เลยค่ะ (เปลี่ยนใจกดอีกปุ่มได้)",
            color=COLOR_MAIN,
        )
        embed.add_field(name=f"✅ มาทำงาน ({len(came)})", value=lines(came), inline=False)
        embed.add_field(name=f"🛌 หยุด ({len(off)})", value=lines(off), inline=False)
        if waiting:
            embed.add_field(
                name=f"⏳ ยังไม่เช็คชื่อ ({len(waiting)})",
                value=", ".join(m.mention for m in waiting)[:1024],
                inline=False,
            )
        return embed

    # ------------------------------------------------------------ ปุ่ม
    async def mark(self, interaction: discord.Interaction, status: str) -> None:
        attendance = self.bot.get_cog("AttendanceCog")
        if await attendance._deny_if_not_staff(interaction):
            return
        day = self._day_of(interaction.message)
        today = dt.datetime.now(self.cfg.tz).date().isoformat()
        if day != today:
            await interaction.response.send_message(
                "กระดานนี้เป็นของวันก่อนแล้วค่ะ ใช้กระดานของวันนี้แทนนะคะ", ephemeral=True
            )
            return

        await self.db.execute(
            "INSERT INTO daily_checkin (day, user_id, status, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day, user_id) DO UPDATE SET status = excluded.status, checked_at = excluded.checked_at",
            (day, interaction.user.id, status, to_iso(now_utc())),
        )
        await interaction.response.edit_message(embed=await self.board_embed(interaction.guild, day))
        text = "✅ เช็คชื่อ **มาทำงาน** วันนี้แล้วค่ะ" if status == STATUS_IN else "🛌 บันทึกว่า **หยุด** วันนี้แล้วค่ะ"
        await interaction.followup.send(text, ephemeral=True)

    # ------------------------------------------------------- โพสต์กระดาน
    async def post_board(self, channel: discord.abc.Messageable) -> discord.Message:
        guild = getattr(channel, "guild", None)
        today = dt.datetime.now(self.cfg.tz).date().isoformat()
        message = await channel.send(embed=await self.board_embed(guild, today), view=DailyCheckView())
        await self.db.set_meta("daily_checkin_last", today)
        return message

    @tasks.loop(minutes=1)
    async def auto_post(self) -> None:
        try:
            if not self.cfg.get("daily_checkin.enabled", True):
                return
            channel_id = self.cfg.channel_id("staff_chat")
            if not channel_id:
                return
            now_local = dt.datetime.now(self.cfg.tz)
            post_at = now_local.replace(
                hour=int(self.cfg.get("daily_checkin.hour", 12)),
                minute=int(self.cfg.get("daily_checkin.minute", 0)),
                second=0,
                microsecond=0,
            )
            today = now_local.date().isoformat()
            if now_local < post_at or await self.db.get_meta("daily_checkin_last") == today:
                return
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                log.warning("ไม่พบห้อง staff-chat (channels.staff_chat) ใน config")
                return
            await self.post_board(channel)
            log.info("โพสต์กระดานเช็คชื่อประจำวัน %s", today)
        except Exception:  # noqa: BLE001 - ไม่ให้ลูปตาย
            log.exception("โพสต์กระดานเช็คชื่อไม่สำเร็จ")

    @auto_post.before_loop
    async def before_auto_post(self) -> None:
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------- คำสั่ง
    @app_commands.command(name="daily_checkin", description="โพสต์กระดานเช็คชื่อพนักงานของวันนี้ในห้องนี้ทันที (แอดมิน)")
    async def daily_checkin(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.post_board(interaction.channel)
        await interaction.followup.send("โพสต์กระดานเช็คชื่อวันนี้แล้วค่ะ", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(DailyCheckView())
    await bot.add_cog(DailyCheckCog(bot))

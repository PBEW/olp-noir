"""คำสั่งดูแลระบบ: ตรวจสถานะ, โหลด config ใหม่"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from core.cycle import cycle_title, next_cutoff_local
from core.embeds import COLOR_INFO, COLOR_OK
from core.utils import fmt_datetime, is_admin


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    def _guard(self, interaction: discord.Interaction) -> bool:
        return is_admin(interaction.user, self.cfg.admin_role_id)

    @app_commands.command(name="health", description="ตรวจสถานะบอทและการตั้งค่า (แอดมิน)")
    async def health(self, interaction: discord.Interaction) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        now_local = dt.datetime.now(self.cfg.tz)
        active = await self.db.active_jobs()
        tickets = await self.db.tickets_by_status(["OPEN", "ACTIVE"])

        def channel_line(key: str) -> str:
            cid = self.cfg.channel_id(key)
            channel = self.bot.get_channel(cid) if cid else None
            return channel.mention if channel else f"⚠️ ยังไม่ได้ตั้งค่า (`channels.{key}`)"

        embed = discord.Embed(title="🩺 สถานะระบบ OLP-Noir", color=COLOR_INFO)
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        embed.add_field(name="เวลาปัจจุบัน", value=fmt_datetime(now_local, self.cfg.tz), inline=True)
        embed.add_field(name="งานที่ยังไม่จบ", value=str(len(active)), inline=True)
        embed.add_field(name="Ticket ที่เปิดอยู่", value=str(len(tickets)), inline=True)
        embed.add_field(name="ห้องแอดมิน", value=channel_line("admin"), inline=False)
        embed.add_field(name="ห้องรีวิว", value=channel_line("review"), inline=False)
        embed.add_field(
            name="Google Sheets",
            value=(
                f"✅ เชื่อมต่อแล้ว · ชีตรอบปัจจุบัน `{await self.db.get_meta('current_cycle') or cycle_title(self.cfg)}`"
                if self.bot.sheets.ready
                else ("⚠️ เปิดใช้งานแต่เชื่อมต่อไม่สำเร็จ" if self.bot.sheets.enabled else "ปิดใช้งาน")
            ),
            inline=False,
        )
        embed.add_field(
            name="ตัดรอบครั้งถัดไป",
            value=fmt_datetime(next_cutoff_local(now_local, self.cfg), self.cfg.tz),
            inline=False,
        )
        tier_lines = [
            f"{t.get('emoji', '')} **{t['name']}** — "
            + (f"<@&{t['role_id']}>" if t.get("role_id") else "⚠️ ยังไม่ตั้ง role_id")
            for t in self.cfg.vip_tiers
        ]
        embed.add_field(
            name="ระดับ VIP", value="\n".join(tier_lines) or "⚠️ ยังไม่ได้ตั้งค่า", inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="reload_config", description="โหลดไฟล์ config.json ใหม่ (แอดมิน)")
    async def reload_config(self, interaction: discord.Interaction) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        self.cfg.reload()
        await interaction.response.send_message(
            embed=discord.Embed(description="โหลด config ใหม่เรียบร้อยค่ะ", color=COLOR_OK),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

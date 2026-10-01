"""เมนูพนักงาน: แผงปุ่มค้างในห้อง รวมเข้างาน ชั่วโมง รายได้ และงานของฉันไว้ที่เดียว"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from core.cycle import cycle_start_local
from core.embeds import COLOR_MAIN, COLOR_OK, STATUS_LABEL
from core.utils import discord_ts, fmt_datetime, from_iso, is_admin, money, now_utc, purge_old_panels, to_iso


class StaffPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @staticmethod
    def _attendance(interaction: discord.Interaction):
        return interaction.client.get_cog("AttendanceCog")

    # แถว 1: ลงเวลา (ตรวจสิทธิ์พนักงานในแต่ละฟังก์ชันอยู่แล้ว)
    @discord.ui.button(label="เข้างานทำงานวันนี้", emoji="🟢", style=discord.ButtonStyle.success, custom_id="olp:staff:in", row=0)
    async def clock_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._attendance(interaction).clock_in(interaction)


    # แถว 2: ข้อมูลของฉัน
    @discord.ui.button(label="ชั่วโมงของฉัน", emoji="🕒", style=discord.ButtonStyle.primary, custom_id="olp:staff:hours", row=1)
    async def my_hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._attendance(interaction).send_my_hours(interaction)

    @discord.ui.button(label="รายได้รอบนี้", emoji="💰", style=discord.ButtonStyle.primary, custom_id="olp:staff:income", row=1)
    async def my_income(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: StaffPanelCog = interaction.client.get_cog("StaffPanelCog")  # type: ignore[assignment]
        await cog.send_my_income(interaction)

    @discord.ui.button(label="งานของฉัน", emoji="📋", style=discord.ButtonStyle.primary, custom_id="olp:staff:jobs", row=1)
    async def my_jobs(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: StaffPanelCog = interaction.client.get_cog("StaffPanelCog")  # type: ignore[assignment]
        await cog.send_my_jobs(interaction)

    # แถว 3: ทีม
    @discord.ui.button(label="ใครอยู่ในกะ", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="olp:staff:on_duty", row=2)
    async def on_duty(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self._attendance(interaction)
        if await cog._deny_if_not_staff(interaction):
            return
        await interaction.response.send_message(embed=await cog.on_duty_embed(), ephemeral=True)


class StaffPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def _deny(self, interaction: discord.Interaction) -> bool:
        return await self.bot.get_cog("AttendanceCog")._deny_if_not_staff(interaction)

    async def send_my_income(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        now_local = dt.datetime.now(self.cfg.tz)
        start = cycle_start_local(now_local, self.cfg)
        jobs = [
            j
            for j in await self.db.jobs_paid_between(to_iso(start), to_iso(now_local))
            if j["staff_id"] == interaction.user.id
        ]
        gross = sum(j["total_price"] for j in jobs)
        share = sum(j["staff_share"] for j in jobs)

        embed = discord.Embed(title="💰 รายได้ของคุณ (รอบปัจจุบัน)", color=COLOR_OK)
        embed.add_field(name="ตั้งแต่", value=fmt_datetime(start, self.cfg.tz), inline=True)
        embed.add_field(name="บิลที่ชำระแล้ว", value=f"{len(jobs)} ใบ", inline=True)
        embed.add_field(name="ยอดบิลรวม", value=money(gross), inline=True)
        donated = 0.0
        donate = self.bot.get_cog("DonateCog")
        if donate is not None:
            donated = (await donate.totals_between(start, now_local))["per_staff"].get(interaction.user.id, 0.0)
        embed.add_field(name="ส่วนแบ่งจากบิล", value=money(share), inline=True)
        embed.add_field(name="🎁 โดเนทที่ได้รับ", value=money(donated), inline=True)
        embed.add_field(name="รวมทั้งหมด", value=f"**{money(share + donated)}**", inline=False)
        if jobs:
            lines = [
                f"`#{j['id']}` {self.cfg.service_names(j['services'])} · แบ่ง {money(j['staff_share'])}"
                for j in jobs[-10:]
            ]
            embed.add_field(name="บิลล่าสุด (สูงสุด 10 ใบ)", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="นับเฉพาะบิลที่ชำระแล้ว · ยอดสุดท้ายยึดตามสรุปตัดรอบของแอดมิน")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def send_my_jobs(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        now = now_utc()
        jobs = [
            j
            for j in await self.db.active_jobs()
            if j["staff_id"] == interaction.user.id and from_iso(j["end_time"]) > now
        ]
        embed = discord.Embed(title="📋 งานของคุณที่ยังไม่จบ", color=COLOR_MAIN)
        if not jobs:
            embed.description = "ตอนนี้ไม่มีงานค้างค่ะ"
        else:
            lines = []
            for j in sorted(jobs, key=lambda j: j["start_time"])[:15]:
                start, end = from_iso(j["start_time"]), from_iso(j["end_time"])
                lines.append(
                    f"`#{j['id']}` <@{j['customer_id']}> · {self.cfg.service_names(j['services'])}\n"
                    f"　{discord_ts(start)}–{discord_ts(end)} · ห้อง {self.cfg.room_name(j.get('room'))} · "
                    f"{STATUS_LABEL.get(j['status'], j['status'])}"
                )
            embed.description = "\n".join(lines)[:4000]
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="panel_staff", description="โพสต์เมนูพนักงาน (เข้างาน, ชั่วโมง, รายได้, งานของฉัน)")
    async def panel_staff(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:staff:")

        embed = discord.Embed(
            title="🧑‍💼 OLP-Noir · เมนูพนักงาน",
            description=(
                "กดปุ่มได้เลย ผลลัพธ์จะเห็นเฉพาะคุณ\n\n"
                "**ลงเวลา** — 🟢 เข้างานทำงานวันนี้ (ไม่ต้องกดออกงาน)\n"
                "**ของฉัน** — 🕒 ชั่วโมงของฉัน · 💰 รายได้รอบนี้ · 📋 งานของฉัน\n"
                "**ทีม** — 👥 ใครอยู่ในกะ\n\n"
                f"*บอทตัดออกงานทุกคนอัตโนมัติตอน {self.bot.get_cog('AttendanceCog')._cutoff_today():%H:%M} น. ค่ะ*"
            ),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=StaffPanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์เมนูพนักงานแล้วค่ะ{note}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(StaffPanel())
    await bot.add_cog(StaffPanelCog(bot))

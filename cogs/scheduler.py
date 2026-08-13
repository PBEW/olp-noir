"""ลูปเบื้องหลัง: แจ้งเตือนเวลางาน, ปิด Ticket ค้าง, VIP หมดอายุ, ตัดรอบรายสัปดาห์"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.cycle import cycle_start_local, cycle_title, next_cutoff_local
from core.embeds import COLOR_INFO, COLOR_OK, COLOR_WARN
from core.utils import (
    discord_ts,
    display_name,
    fmt_datetime,
    from_iso,
    is_admin,
    money,
    now_utc,
    send_dm,
    to_iso,
)

log = logging.getLogger("olp.scheduler")

RUNNING_STATUSES = ["PAID"]  # แจ้งเตือนเวลาเฉพาะบิลที่ชำระเงินแล้วเท่านั้น


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def cog_load(self) -> None:
        self.tick.change_interval(seconds=self.cfg.loop_seconds)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ------------------------------------------------------------- main loop
    @tasks.loop(seconds=35)
    async def tick(self) -> None:
        try:
            await self.check_jobs()
            await self.check_tickets()
            await self.check_vip_expiry()
            await self.check_cutoff()
        except Exception:  # noqa: BLE001 - ลูปต้องไม่ตาย
            log.exception("เกิดข้อผิดพลาดใน background loop")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()
        # ตั้งค่ารอบปัจจุบันครั้งแรก (ไม่ส่งสรุปย้อนหลัง)
        if await self.db.get_meta("current_cycle") is None:
            now_local = dt.datetime.now(self.cfg.tz)
            await self.db.set_meta("current_cycle", cycle_title(self.cfg, now_local))
            await self.db.set_meta(
                "last_cutoff", cycle_start_local(now_local, self.cfg).date().isoformat()
            )

    # ------------------------------------------------------- แจ้งเตือนเวลางาน
    async def check_jobs(self) -> None:
        now = now_utc()
        before_start = dt.timedelta(minutes=self.cfg.before_start_minutes)
        before_end = dt.timedelta(minutes=self.cfg.before_end_minutes)

        for job in await self.db.jobs_by_status(RUNNING_STATUSES):
            start, end = from_iso(job["start_time"]), from_iso(job["end_time"])

            if not job["notified_start"] and now >= start - before_start:
                await self.db.update_job(job["id"], notified_start=1)
                if now < end:
                    await self._notify_start(job, start)

            if not job["notified_end"] and now >= end - before_end:
                await self.db.update_job(job["id"], notified_end=1)
                await self._notify_end(job, end)

            if now >= end and not job["review_sent"]:
                await self.db.update_job(job["id"], review_sent=1)
                if job["status"] == "PAID":
                    await self.db.update_job(job["id"], status="COMPLETED")
                    if job["job_type"] == "NORMAL":
                        reviews = self.bot.get_cog("ReviewsCog")
                        await reviews.send_review_invite(job)

    async def _notify_start(self, job: dict, start: dt.datetime) -> None:
        minutes = self.cfg.before_start_minutes
        await send_dm(
            self.bot,
            job["staff_id"],
            embed=discord.Embed(
                title=f"⏰ อีก {minutes} นาทีจะถึงเวลางาน",
                description=(
                    f"บิล `#{job['id']}` · ลูกค้า <@{job['customer_id']}>\n"
                    f"บริการ: {self.cfg.service_names(job['services'])}\n"
                    f"ห้อง: {self.cfg.room_name(job.get('room'))}\n"
                    f"เริ่ม {discord_ts(start)} — เตรียมตัวได้เลยค่ะ"
                ),
                color=COLOR_WARN,
            ),
        )
        await send_dm(
            self.bot,
            job["customer_id"],
            embed=discord.Embed(
                title=f"⏰ อีก {minutes} นาทีจะถึงเวลานัด",
                description=(
                    f"บิล `#{job['id']}` · พนักงาน <@{job['staff_id']}>\n"
                    f"ห้อง: {self.cfg.room_name(job.get('room'))}\n"
                    f"เริ่ม {discord_ts(start)} — เตรียมเข้างานได้เลยค่ะ"
                ),
                color=COLOR_WARN,
            ),
        )

    async def _notify_end(self, job: dict, end: dt.datetime) -> None:
        minutes = self.cfg.before_end_minutes
        embed = discord.Embed(
            title=f"⌛ อีก {minutes} นาทีจะหมดเวลา",
            description=(
                f"บิล `#{job['id']}` จะจบเวลา {discord_ts(end)}\n"
                "หากต้องการต่อเวลา แจ้งแอดมินเพื่อเปิดบิลต่อเวลาได้เลยค่ะ"
            ),
            color=COLOR_WARN,
        )
        await send_dm(self.bot, job["staff_id"], embed=embed)
        await send_dm(self.bot, job["customer_id"], embed=embed)

    # ------------------------------------------------------------- Ticket
    async def check_tickets(self) -> None:
        limit = dt.timedelta(minutes=self.cfg.ticket_timeout_minutes)
        now = now_utc()
        tickets = self.bot.get_cog("TicketsCog")

        for ticket in await self.db.tickets_by_status(["OPEN", "ACTIVE"]):
            last = from_iso(ticket["last_activity"])
            if last is None or now - last < limit:
                continue
            reason = (
                f"ไม่มีการสนทนาเกิน {self.cfg.ticket_timeout_minutes} นาที ระบบจึงปิดอัตโนมัติ"
                if ticket["status"] == "ACTIVE"
                else f"ไม่มีแอดมินรับเรื่องภายใน {self.cfg.ticket_timeout_minutes} นาที"
            )
            await tickets.close_ticket(ticket["id"], reason=reason)

    # ---------------------------------------------------------------- VIP
    async def check_vip_expiry(self) -> None:
        now_iso = to_iso(now_utc())
        expired = await self.db.fetchall(
            "SELECT * FROM vip_members WHERE expires_at IS NOT NULL AND expires_at <= ?", (now_iso,)
        )
        if not expired:
            return
        vip = self.bot.get_cog("VipCog")
        for record in expired:
            await vip.expire_pass(record)

    # ------------------------------------------------------------ ตัดรอบ
    async def check_cutoff(self) -> None:
        now_local = dt.datetime.now(self.cfg.tz)
        current_start = cycle_start_local(now_local, self.cfg)
        last = await self.db.get_meta("last_cutoff")
        if last == current_start.date().isoformat():
            return
        await self.run_cutoff(now_local)

    async def run_cutoff(self, now_local: dt.datetime | None = None) -> discord.Embed:
        now_local = now_local or dt.datetime.now(self.cfg.tz)
        current_start = cycle_start_local(now_local, self.cfg)
        previous_start = current_start - dt.timedelta(days=7)

        summary = await self.build_summary(previous_start, current_start)

        new_title = cycle_title(self.cfg, now_local)
        created = await self.bot.sheets.create_cycle_sheet(new_title)
        await self.db.set_meta("current_cycle", new_title)
        await self.db.set_meta("last_cutoff", current_start.date().isoformat())

        summary.add_field(
            name="ชีตรอบใหม่",
            value=f"`{new_title}`" + ("" if created else " *(ยังไม่ได้เปิดใช้ Google Sheets)*"),
            inline=False,
        )
        summary.add_field(
            name="ตัดรอบครั้งถัดไป",
            value=fmt_datetime(next_cutoff_local(now_local, self.cfg), self.cfg.tz),
            inline=False,
        )

        payments = self.bot.get_cog("PaymentsCog")
        await payments.notify_admin(embed=summary)
        log.info("ตัดรอบเรียบร้อย -> %s", new_title)
        return summary

    async def build_summary(self, start_local: dt.datetime, end_local: dt.datetime) -> discord.Embed:
        jobs = await self.db.jobs_paid_between(to_iso(start_local), to_iso(end_local))
        total_in = sum(j["total_price"] for j in jobs)
        total_out = sum(j["staff_share"] for j in jobs)
        total_shop = sum(j["shop_share"] for j in jobs)

        per_staff: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for job in jobs:
            row = per_staff[job["staff_id"]]
            row[0] += job["total_price"]
            row[1] += job["staff_share"]
            row[2] += 1

        embed = discord.Embed(
            title="📊 สรุปยอดรอบบิล",
            description=(
                f"รอบวันที่ **{start_local:%d/%m/%Y}** ถึง **{end_local:%d/%m/%Y}**\n"
                f"จำนวนบิลที่ชำระแล้ว: **{len(jobs)}** ใบ"
            ),
            color=COLOR_OK,
        )
        embed.add_field(name="รายรับรวม (In)", value=money(total_in), inline=True)
        embed.add_field(name="ส่วนแบ่งพนักงาน (Out)", value=money(total_out), inline=True)
        embed.add_field(name="รายได้เข้าร้าน", value=money(total_shop), inline=True)

        guild = self.bot.get_guild(self.cfg.guild_id)
        if per_staff:
            lines = []
            for staff_id, (gross, share, count) in sorted(
                per_staff.items(), key=lambda kv: kv[1][0], reverse=True
            ):
                name = await display_name(self.bot, guild, staff_id)
                lines.append(f"• **{name}** — {count} บิล · In {money(gross)} · แบ่ง {money(share)}")
            embed.add_field(name="แยกตามพนักงาน", value="\n".join(lines)[:1024], inline=False)

        url = await self.bot.sheets.spreadsheet_url()
        if url:
            embed.add_field(name="Google Sheets", value=url, inline=False)
        return embed

    # ---------------------------------------------------------- คำสั่ง
    @app_commands.command(name="cutoff", description="ตัดรอบบัญชีทันที (แอดมิน)")
    async def cutoff_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.run_cutoff()
        await interaction.followup.send("ตัดรอบเรียบร้อย ส่งสรุปเข้าห้องแอดมินแล้วค่ะ", ephemeral=True)

    @app_commands.command(name="summary", description="ดูสรุปยอดของรอบปัจจุบัน (แอดมิน)")
    async def summary_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        now_local = dt.datetime.now(self.cfg.tz)
        start = cycle_start_local(now_local, self.cfg)
        embed = await self.build_summary(start, now_local)
        embed.title = "📊 สรุปยอดรอบปัจจุบัน"
        embed.color = COLOR_INFO
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SchedulerCog(bot))

"""ระบบเข้างานพนักงาน: แผงปุ่มเข้า/ออกงาน, Role On Duty, เตือนลืมออกงาน, สรุปชั่วโมง"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.cycle import cycle_start_local
from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN
from core.utils import (
    TimeParseError,
    discord_ts,
    display_name,
    fmt_datetime,
    fmt_time,
    from_iso,
    is_admin,
    now_utc,
    parse_start_time,
    purge_old_panels,
    send_dm,
    to_iso,
)

log = logging.getLogger("olp.attendance")


def fmt_hours(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} ชม. {minutes % 60:02d} นาที"


def parse_past_time(raw: str, tz) -> dt.datetime:
    """อ่านเวลาที่ผ่านมาแล้ว (ใช้ตอนแอดมินแก้เวลา) — เวลาแบบไม่ระบุวันที่อยู่ในอนาคตจะถือเป็นของเมื่อวาน"""
    value = parse_start_time(raw, tz)
    now = now_utc()
    if value > now + dt.timedelta(minutes=1):
        value -= dt.timedelta(days=1)
    if value > now + dt.timedelta(minutes=1):
        raise TimeParseError("เวลาที่ระบุอยู่ในอนาคต")
    return value


class AttendancePanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="เข้างาน",
        emoji="🟢",
        style=discord.ButtonStyle.success,
        custom_id="olp:attendance:in",
    )
    async def clock_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("AttendanceCog")
        await cog.clock_in(interaction)

    @discord.ui.button(
        label="ออกงาน",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
        custom_id="olp:attendance:out",
    )
    async def clock_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("AttendanceCog")
        await cog.clock_out(interaction)

    @discord.ui.button(
        label="ชั่วโมงของฉัน",
        emoji="🕒",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:attendance:hours",
    )
    async def my_hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("AttendanceCog")
        await cog.send_my_hours(interaction)


class AttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def cog_load(self) -> None:
        self.watch_open_shifts.start()

    async def cog_unload(self) -> None:
        self.watch_open_shifts.cancel()

    # -------------------------------------------------------------- สิทธิ์
    def _is_staff(self, member: discord.abc.User) -> bool:
        if is_admin(member, self.cfg.admin_role_id):
            return True
        role_id = self.cfg.staff_role_id
        return isinstance(member, discord.Member) and any(r.id == role_id for r in member.roles)

    async def _deny_if_not_staff(self, interaction: discord.Interaction) -> bool:
        if self._is_staff(interaction.user):
            return False
        if not self.cfg.staff_role_id:
            msg = "ยังไม่ได้ตั้งค่า `roles.staff` ใน config.json แจ้งแอดมินก่อนนะคะ"
        else:
            msg = "ปุ่มนี้สำหรับพนักงานเท่านั้นค่ะ"
        await interaction.response.send_message(msg, ephemeral=True)
        return True

    # ---------------------------------------------------------- Role On Duty
    async def _set_on_duty(self, guild: discord.Guild | None, user_id: int, on: bool) -> None:
        role_id = self.cfg.on_duty_role_id
        if guild is None or not role_id:
            return
        role = guild.get_role(role_id)
        member = guild.get_member(user_id)
        if role is None or member is None:
            return
        try:
            if on and role not in member.roles:
                await member.add_roles(role, reason="เข้างาน")
            elif not on and role in member.roles:
                await member.remove_roles(role, reason="ออกงาน")
        except discord.HTTPException as exc:
            log.warning("ปรับ Role On Duty ของ %s ไม่สำเร็จ: %s", user_id, exc)

    # ------------------------------------------------------------ เข้า/ออก
    async def clock_in(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_staff(interaction):
            return
        user = interaction.user
        current = await self.db.open_attendance(user.id)
        if current is not None:
            started = from_iso(current["clock_in"])
            await interaction.response.send_message(
                f"คุณเข้างานอยู่แล้วตั้งแต่ {discord_ts(started, 'f')} ({discord_ts(started, 'R')}) ค่ะ",
                ephemeral=True,
            )
            return

        now = now_utc()
        row_id = await self.db.create_attendance(interaction.guild_id or self.cfg.guild_id, user.id, to_iso(now))
        await interaction.response.send_message(
            f"🟢 บันทึกเข้างานแล้ว เวลา **{fmt_time(now, self.cfg.tz)}** น. ขอให้เป็นกะที่ดีนะคะ",
            ephemeral=True,
        )
        await self._set_on_duty(interaction.guild, user.id, True)
        await self._notify_admin(f"🟢 <@{user.id}> เข้างาน {discord_ts(now)} (กะ `#{row_id}`)")

    async def clock_out(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_staff(interaction):
            return
        user = interaction.user
        current = await self.db.open_attendance(user.id)
        if current is None:
            await interaction.response.send_message(
                "คุณยังไม่ได้กดเข้างานค่ะ (ถ้าลืมกด แจ้งแอดมินให้แก้เวลาได้นะคะ)", ephemeral=True
            )
            return

        now = now_utc()
        await self.db.update_attendance(current["id"], clock_out=to_iso(now))
        seconds = (now - from_iso(current["clock_in"])).total_seconds()
        await interaction.response.send_message(
            f"🔴 บันทึกออกงานแล้ว เวลา **{fmt_time(now, self.cfg.tz)}** น. · "
            f"กะนี้ทำงาน **{fmt_hours(seconds)}** ขอบคุณค่ะ",
            ephemeral=True,
        )
        await self._set_on_duty(interaction.guild, user.id, False)
        await self._notify_admin(
            f"🔴 <@{user.id}> ออกงาน {discord_ts(now)} · {fmt_hours(seconds)} (กะ `#{current['id']}`)"
        )
        await self._log_to_sheet(await self.db.get_attendance(current["id"]))

    # ------------------------------------------------------------ ชั่วโมง
    async def hours_by_user(
        self, start: dt.datetime, end: dt.datetime, user_id: int | None = None
    ) -> dict[int, list[float]]:
        """{user_id: [วินาทีรวม, จำนวนกะ]} โดยตัดกะให้อยู่ในช่วงเวลา (กะที่ยังเปิดนับถึงตอนนี้)"""
        rows = await self.db.attendance_overlapping(to_iso(start), to_iso(end), user_id)
        now = now_utc()
        result: dict[int, list[float]] = defaultdict(lambda: [0.0, 0])
        for row in rows:
            s = max(from_iso(row["clock_in"]), start)
            e = min(from_iso(row["clock_out"]) or now, end)
            if e > s:
                result[row["user_id"]][0] += (e - s).total_seconds()
                result[row["user_id"]][1] += 1
        return result

    async def build_hours_summary(
        self, start_local: dt.datetime, end_local: dt.datetime, *, title: str = "🕒 สรุปชั่วโมงงานพนักงาน"
    ) -> discord.Embed:
        totals = await self.hours_by_user(start_local, end_local)
        embed = discord.Embed(
            title=title,
            description=f"ช่วง **{fmt_datetime(start_local, self.cfg.tz)}** ถึง **{fmt_datetime(end_local, self.cfg.tz)}**",
            color=COLOR_INFO,
        )
        if not totals:
            embed.add_field(name="ผลรวม", value="ไม่มีบันทึกเข้างานในช่วงนี้", inline=False)
            return embed

        guild = self.bot.get_guild(self.cfg.guild_id)
        lines = []
        for user_id, (seconds, count) in sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True):
            name = await display_name(self.bot, guild, user_id)
            lines.append(f"• **{name}** — {fmt_hours(seconds)} ({int(count)} กะ)")
        embed.add_field(name="แยกตามพนักงาน", value="\n".join(lines)[:1024], inline=False)
        embed.add_field(
            name="รวมทั้งหมด", value=fmt_hours(sum(v[0] for v in totals.values())), inline=False
        )
        return embed

    async def send_my_hours(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_staff(interaction):
            return
        now_local = dt.datetime.now(self.cfg.tz)
        start = cycle_start_local(now_local, self.cfg)
        totals = await self.hours_by_user(start, now_local, interaction.user.id)
        seconds, count = totals.get(interaction.user.id, [0.0, 0])

        embed = discord.Embed(title="🕒 ชั่วโมงงานของคุณ (รอบปัจจุบัน)", color=COLOR_MAIN)
        embed.add_field(name="ตั้งแต่", value=fmt_datetime(start, self.cfg.tz), inline=True)
        embed.add_field(name="รวม", value=f"**{fmt_hours(seconds)}** ({int(count)} กะ)", inline=True)
        current = await self.db.open_attendance(interaction.user.id)
        embed.add_field(
            name="สถานะ",
            value=(
                f"🟢 กำลังทำงาน (เข้างาน {discord_ts(from_iso(current['clock_in']), 'R')})"
                if current
                else "⚪ ไม่ได้อยู่ในกะ"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --------------------------------------------------- เตือน/ปิดกะค้าง
    @tasks.loop(minutes=5)
    async def watch_open_shifts(self) -> None:
        try:
            await self._check_open_shifts()
        except Exception:  # noqa: BLE001 - ไม่ให้ลูปตาย
            log.exception("ตรวจกะค้างไม่สำเร็จ")

    @watch_open_shifts.before_loop
    async def before_watch(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_open_shifts(self) -> None:
        now = now_utc()
        warn_after = dt.timedelta(hours=self.cfg.attendance_warn_hours)
        close_after = dt.timedelta(hours=self.cfg.attendance_auto_close_hours)
        guild = self.bot.get_guild(self.cfg.guild_id)

        for row in await self.db.all_open_attendance():
            started = from_iso(row["clock_in"])
            elapsed = now - started

            if elapsed >= close_after:
                # ปิดที่เวลาเข้างาน + ชั่วโมงเตือน เพื่อไม่ให้ชั่วโมงบวมเกินจริง แอดมินแก้ได้ภายหลัง
                end = started + warn_after
                await self.db.update_attendance(
                    row["id"], clock_out=to_iso(end), auto_closed=1, note="บอทปิดกะอัตโนมัติ (ลืมออกงาน)"
                )
                await self._set_on_duty(guild, row["user_id"], False)
                await send_dm(
                    self.bot,
                    row["user_id"],
                    content=(
                        f"⚠️ บอทปิดกะ `#{row['id']}` ให้อัตโนมัติ เพราะไม่ได้กดออกงานเกิน "
                        f"{self.cfg.attendance_auto_close_hours:g} ชม. — ถ้าเวลาไม่ถูกต้อง แจ้งแอดมินให้แก้นะคะ"
                    ),
                )
                await self._notify_admin(
                    embed=discord.Embed(
                        title="⚠️ ปิดกะอัตโนมัติ (ลืมออกงาน)",
                        description=(
                            f"พนักงาน <@{row['user_id']}> · กะ `#{row['id']}`\n"
                            f"เข้างาน {discord_ts(started, 'f')} → บันทึกออกงาน {discord_ts(end, 'f')}\n"
                            f"แก้เวลาได้ด้วย `/attendance_fix`"
                        ),
                        color=COLOR_DANGER,
                    )
                )
                await self._log_to_sheet(await self.db.get_attendance(row["id"]))
            elif elapsed >= warn_after and not row["warned"]:
                await self.db.update_attendance(row["id"], warned=1)
                await send_dm(
                    self.bot,
                    row["user_id"],
                    content=(
                        f"⏰ คุณเข้างานมาแล้ว {fmt_hours(elapsed.total_seconds())} "
                        f"(ตั้งแต่ {discord_ts(started, 'f')}) ลืมกดออกงานหรือเปล่าคะ?"
                    ),
                )

    # ------------------------------------------------------------ helpers
    async def _notify_admin(self, content: str | None = None, *, embed: discord.Embed | None = None) -> None:
        payments = self.bot.get_cog("PaymentsCog")
        if payments is not None:
            await payments.notify_admin(content=content, embed=embed)

    async def _log_to_sheet(self, row: dict | None) -> None:
        if row is None or not row["clock_out"]:
            return
        tz = self.cfg.tz
        start, end = from_iso(row["clock_in"]), from_iso(row["clock_out"])
        guild = self.bot.get_guild(self.cfg.guild_id)
        await self.bot.sheets.append_attendance_row(
            [
                row["id"],
                start.astimezone(tz).strftime("%d/%m/%Y"),
                fmt_datetime(start, tz),
                fmt_datetime(end, tz),
                await display_name(self.bot, guild, row["user_id"]),
                str(row["user_id"]),
                round((end - start).total_seconds() / 3600, 2),
                row.get("note") or "",
            ]
        )

    # ---------------------------------------------------------- คำสั่ง
    @app_commands.command(name="panel_attendance", description="โพสต์แผงเข้างาน/ออกงานสำหรับพนักงาน (แอดมิน)")
    async def panel_attendance(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:attendance:")

        embed = discord.Embed(
            title="🕒 OLP-Noir · ลงเวลาทำงาน",
            description=(
                "🟢 **เข้างาน** — กดเมื่อเริ่มกะ\n"
                "🔴 **ออกงาน** — กดเมื่อจบกะ\n"
                "🕒 **ชั่วโมงของฉัน** — ดูชั่วโมงสะสมของรอบนี้\n\n"
                f"*ลืมกดออกงานเกิน {self.cfg.attendance_warn_hours:g} ชม. บอทจะเตือนทาง DM ค่ะ*"
            ),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=AttendancePanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์แผงเข้างานแล้วค่ะ{note}", ephemeral=True)

    @app_commands.command(name="my_hours", description="ดูชั่วโมงงานของคุณในรอบปัจจุบัน")
    async def my_hours_command(self, interaction: discord.Interaction) -> None:
        await self.send_my_hours(interaction)

    async def on_duty_embed(self) -> discord.Embed:
        rows = await self.db.all_open_attendance()
        if not rows:
            return discord.Embed(description="ตอนนี้ไม่มีพนักงานอยู่ในกะค่ะ", color=COLOR_MAIN)
        lines = [
            f"• <@{r['user_id']}> — เข้างาน {discord_ts(from_iso(r['clock_in']), 'R')}"
            for r in sorted(rows, key=lambda r: r["clock_in"])
        ]
        return discord.Embed(title="🟢 พนักงานที่อยู่ในกะ", description="\n".join(lines)[:4000], color=COLOR_OK)

    @app_commands.command(name="on_duty", description="ดูว่าตอนนี้พนักงานคนไหนอยู่ในกะบ้าง")
    async def on_duty_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self.on_duty_embed(), ephemeral=True)

    @app_commands.command(name="attendance_report", description="สรุปชั่วโมงงานพนักงานของรอบปัจจุบัน (แอดมิน)")
    async def attendance_report(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await self.current_hours_embed(), ephemeral=True)

    async def current_hours_embed(self) -> discord.Embed:
        now_local = dt.datetime.now(self.cfg.tz)
        return await self.build_hours_summary(
            cycle_start_local(now_local, self.cfg), now_local, title="🕒 ชั่วโมงงานรอบปัจจุบัน"
        )

    @app_commands.command(name="attendance_fix", description="แก้เวลาเข้า/ออกงานของกะล่าสุดของพนักงาน (แอดมิน)")
    @app_commands.describe(
        member="พนักงานที่ต้องการแก้",
        clock_in="เวลาเข้างานใหม่ เช่น 18:00 หรือ 05/09 18:00 (เว้นว่าง = ไม่แก้)",
        clock_out="เวลาออกงานใหม่ เช่น 02:30 หรือ 06/09 02:30 (เว้นว่าง = ไม่แก้)",
        new_shift="สร้างกะใหม่แทนการแก้กะล่าสุด (กรณีลืมกดเข้างานทั้งกะ)",
    )
    async def attendance_fix(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        clock_in: str | None = None,
        clock_out: str | None = None,
        new_shift: bool = False,
    ) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await self.fix_attendance(interaction, member, clock_in, clock_out, new_shift)

    async def fix_attendance(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        clock_in: str | None,
        clock_out: str | None,
        new_shift: bool = False,
    ) -> None:
        """แก้เวลาเข้างาน (ใช้ทั้งจาก /attendance_fix และเมนูแอดมิน) — ผู้เรียกต้องตรวจสิทธิ์แอดมินก่อน"""
        if not clock_in and not clock_out:
            await interaction.response.send_message("ระบุ clock_in หรือ clock_out อย่างน้อย 1 ค่าค่ะ", ephemeral=True)
            return

        tz = self.cfg.tz
        try:
            new_in = parse_past_time(clock_in, tz) if clock_in else None
            new_out = parse_past_time(clock_out, tz) if clock_out else None
        except TimeParseError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        if new_shift:
            if new_in is None or new_out is None:
                await interaction.response.send_message("สร้างกะใหม่ต้องระบุทั้ง clock_in และ clock_out ค่ะ", ephemeral=True)
                return
            row_id = await self.db.create_attendance(member.guild.id, member.id, to_iso(new_in))
            row = await self.db.get_attendance(row_id)
        else:
            row = await self.db.latest_attendance(member.id)
            if row is None:
                await interaction.response.send_message(
                    "พนักงานคนนี้ยังไม่มีบันทึกเข้างาน ใช้ `new_shift: True` เพื่อสร้างกะใหม่ค่ะ", ephemeral=True
                )
                return

        final_in = new_in or from_iso(row["clock_in"])
        final_out = new_out or from_iso(row["clock_out"])
        if final_out is not None and final_out <= final_in:
            if new_shift:
                await self.db.execute("DELETE FROM attendance WHERE id = ?", (row["id"],))
            await interaction.response.send_message("❌ เวลาออกงานต้องอยู่หลังเวลาเข้างานค่ะ", ephemeral=True)
            return

        await self.db.update_attendance(
            row["id"],
            clock_in=to_iso(final_in),
            clock_out=to_iso(final_out) if final_out else None,
            edited_by=interaction.user.id,
            note=f"แอดมิน {interaction.user.display_name} แก้เวลา",
        )
        if final_out is not None:
            await self._set_on_duty(member.guild, member.id, False)

        summary = (
            f"กะ `#{row['id']}` ของ {member.mention}\n"
            f"เข้างาน {discord_ts(final_in, 'f')} → "
            + (f"ออกงาน {discord_ts(final_out, 'f')} ({fmt_hours((final_out - final_in).total_seconds())})" if final_out else "ยังไม่ออกงาน")
        )
        await interaction.response.send_message(f"✅ แก้เวลาแล้ว\n{summary}", ephemeral=True)
        await self._notify_admin(
            embed=discord.Embed(
                title="✏️ แก้เวลาเข้างาน",
                description=f"{summary}\nโดย {interaction.user.mention}",
                color=COLOR_WARN,
            )
        )
        if final_out is not None:
            await self._log_to_sheet(await self.db.get_attendance(row["id"]))


async def setup(bot: commands.Bot) -> None:
    bot.add_view(AttendancePanel())
    await bot.add_cog(AttendanceCog(bot))

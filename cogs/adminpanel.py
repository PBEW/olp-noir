"""เมนูแอดมิน: แผงปุ่มค้างในห้อง รวมงานแอดมินที่ใช้บ่อยไว้ที่เดียว ไม่ต้องจำคำสั่ง"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN
from core.utils import is_admin, purge_old_panels

NOT_ADMIN = "เฉพาะแอดมินเท่านั้นค่ะ"


def _is_admin(interaction: discord.Interaction) -> bool:
    return is_admin(interaction.user, interaction.client.cfg.admin_role_id)


def _member(interaction: discord.Interaction, user: discord.abc.User) -> discord.Member | None:
    if isinstance(user, discord.Member):
        return user
    return interaction.guild.get_member(user.id) if interaction.guild else None


class AdminOnlyView(discord.ui.View):
    """View ชั่วคราว (เห็นคนเดียว) ที่ตรวจสิทธิ์แอดมินทุกครั้งที่กด"""

    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_admin(interaction):
            return True
        await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
        return False


# ---------------------------------------------------------------- ให้ VIP
class VipGrantView(AdminOnlyView):
    def __init__(self, cfg) -> None:
        super().__init__()
        self.member: discord.Member | None = None
        self.tier_key: str | None = None
        self.months = 1

        tiers = cfg.vip_tiers
        self.tier_select.options = [
            discord.SelectOption(label=t["name"], value=t["key"], emoji=t.get("emoji") or None)
            for t in tiers
        ] or [discord.SelectOption(label="ยังไม่ได้ตั้งค่า vip_tiers", value="-")]
        self.months_select.options = [
            discord.SelectOption(label=f"{m} เดือน", value=str(m), default=m == 1)
            for m in (1, 2, 3, 6, 12)
        ]

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="1) เลือกสมาชิก", row=0)
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.member = _member(interaction, select.values[0])
        await interaction.response.defer()

    @discord.ui.select(placeholder="2) เลือกระดับ VIP", row=1)
    async def tier_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.tier_key = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(placeholder="3) จำนวนเดือน (ค่าเริ่มต้น 1 เดือน)", row=2)
    async def months_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.months = int(select.values[0])
        await interaction.response.defer()

    @discord.ui.button(label="ยืนยันให้สิทธิ์", emoji="✅", style=discord.ButtonStyle.success, row=3)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.member is None or not self.tier_key or self.tier_key == "-":
            await interaction.response.send_message("เลือกสมาชิกและระดับ VIP ให้ครบก่อนค่ะ", ephemeral=True)
            return
        self.stop()
        await interaction.client.get_cog("VipCog").grant_vip(interaction, self.member, self.tier_key, self.months)


# ------------------------------------------------------- แก้เวลาเข้างาน
class AttendanceFixModal(discord.ui.Modal):
    clock_in = discord.ui.TextInput(
        label="เวลาเข้างาน (เว้นว่าง = ไม่แก้)",
        placeholder="เช่น 18:00 หรือ 05/09 18:00",
        required=False,
        max_length=20,
    )
    clock_out = discord.ui.TextInput(
        label="เวลาออกงาน (เว้นว่าง = ไม่แก้)",
        placeholder="เช่น 02:30 หรือ 06/09 02:30",
        required=False,
        max_length=20,
    )

    def __init__(self, member: discord.Member, new_shift: bool) -> None:
        title = "เพิ่มกะที่ลืมกด" if new_shift else "แก้เวลากะล่าสุด"
        super().__init__(title=f"{title} · {member.display_name}"[:45])
        self.member = member
        self.new_shift = new_shift
        if new_shift:
            self.clock_in.label = "เวลาเข้างาน"
            self.clock_out.label = "เวลาออกงาน"
            self.clock_in.required = self.clock_out.required = True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
            return
        await interaction.client.get_cog("AttendanceCog").fix_attendance(
            interaction,
            self.member,
            self.clock_in.value.strip() or None,
            self.clock_out.value.strip() or None,
            self.new_shift,
        )


class AttendanceFixView(AdminOnlyView):
    def __init__(self) -> None:
        super().__init__()
        self.member: discord.Member | None = None

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="เลือกพนักงาน", row=0)
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.member = _member(interaction, select.values[0])
        await interaction.response.defer()

    async def _open(self, interaction: discord.Interaction, new_shift: bool) -> None:
        if self.member is None:
            await interaction.response.send_message("เลือกพนักงานก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(AttendanceFixModal(self.member, new_shift))

    @discord.ui.button(label="แก้กะล่าสุด", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit_latest(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, False)

    @discord.ui.button(label="เพิ่มกะที่ลืมกด", emoji="➕", style=discord.ButtonStyle.secondary, row=1)
    async def add_shift(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, True)


# ------------------------------------------------------------- ตัดรอบ
class CutoffConfirmView(AdminOnlyView):
    @discord.ui.button(label="ยืนยันตัดรอบ", emoji="✂️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="⏳ กำลังตัดรอบ...", color=COLOR_WARN), view=None
        )
        await interaction.client.get_cog("SchedulerCog").run_cutoff()
        await interaction.edit_original_response(
            embed=discord.Embed(description="✅ ตัดรอบเรียบร้อย ส่งสรุปเข้าห้องแอดมินแล้วค่ะ", color=COLOR_OK)
        )

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="ยกเลิกการตัดรอบแล้วค่ะ", color=COLOR_MAIN), view=None
        )


# ---------------------------------------------------------- แผงหลัก
HELP_TEXT = (
    "**แผงที่โพสต์ได้**\n"
    "`/panel reception` แผงรีเซปชั่น (เปิดบิล / ต่อเวลา / งานที่ดำเนินอยู่)\n"
    "`/panel_request` แผงบริการลูกค้า · `/panel_staff` เมนูพนักงาน · `/panel_attendance` แผงลงเวลา · `/panel_admin` แผงนี้\n\n"
    "**บิล**\n"
    "`/bill info` ดูบิล · `/bill paid` ยืนยันชำระด้วยมือ · `/bill cancel` ยกเลิกบิล\n\n"
    "**อื่น ๆ**\n"
    "`/vip_grant` ให้ VIP · `/attendance_fix` แก้เวลาเข้างาน · `/cutoff` ตัดรอบ · `/summary` สรุปยอด\n"
    "`/attendance_report` ชั่วโมงงาน · `/on_duty` ใครอยู่ในกะ · `/health` สถานะระบบ · `/reload_config` โหลด config"
)


class AdminPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_admin(interaction):
            return True
        await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
        return False

    # แถว 1: ดูข้อมูล
    @discord.ui.button(label="สรุปยอดรอบนี้", emoji="📊", style=discord.ButtonStyle.primary, custom_id="olp:admin:summary", row=0)
    async def summary(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await interaction.client.get_cog("SchedulerCog").current_summary()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="ชั่วโมงงาน", emoji="🕒", style=discord.ButtonStyle.primary, custom_id="olp:admin:hours", row=0)
    async def hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await interaction.client.get_cog("AttendanceCog").current_hours_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="ใครอยู่ในกะ", emoji="🟢", style=discord.ButtonStyle.primary, custom_id="olp:admin:on_duty", row=0)
    async def on_duty(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = await interaction.client.get_cog("AttendanceCog").on_duty_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # แถว 2: จัดการ
    @discord.ui.button(label="ให้สิทธิ์ VIP", emoji="💎", style=discord.ButtonStyle.success, custom_id="olp:admin:vip", row=1)
    async def vip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="💎 ให้สิทธิ์ VIP ด้วยมือ",
                description="เลือกสมาชิก → ระดับ → จำนวนเดือน แล้วกด **ยืนยันให้สิทธิ์**",
                color=COLOR_INFO,
            ),
            view=VipGrantView(interaction.client.cfg),
            ephemeral=True,
        )

    @discord.ui.button(label="แก้เวลาเข้างาน", emoji="✏️", style=discord.ButtonStyle.success, custom_id="olp:admin:fix", row=1)
    async def fix(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="✏️ แก้เวลาเข้างาน",
                description=(
                    "เลือกพนักงาน แล้วเลือก\n"
                    "✏️ **แก้กะล่าสุด** — ลืมกดออกงาน หรือเวลาไม่ถูก\n"
                    "➕ **เพิ่มกะที่ลืมกด** — ลืมกดทั้งเข้าและออกงาน"
                ),
                color=COLOR_INFO,
            ),
            view=AttendanceFixView(),
            ephemeral=True,
        )

    @discord.ui.button(label="ตัดรอบทันที", emoji="✂️", style=discord.ButtonStyle.danger, custom_id="olp:admin:cutoff", row=1)
    async def cutoff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="✂️ ยืนยันตัดรอบ?",
                description="บอทจะสรุปยอดรอบนี้ส่งเข้าห้องแอดมินและเริ่มรอบใหม่ **ย้อนกลับไม่ได้**",
                color=COLOR_DANGER,
            ),
            view=CutoffConfirmView(),
            ephemeral=True,
        )

    # แถว 3: ระบบ
    @discord.ui.button(label="สถานะระบบ", emoji="🩺", style=discord.ButtonStyle.secondary, custom_id="olp:admin:health", row=2)
    async def health(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = await interaction.client.get_cog("AdminCog").health_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="โหลด config ใหม่", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="olp:admin:reload", row=2)
    async def reload(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            interaction.client.cfg.reload()
        except ValueError as exc:  # JSON ผิดรูปแบบ
            await interaction.response.send_message(f"❌ อ่าน config.json ไม่ได้: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(description="โหลด config ใหม่เรียบร้อยค่ะ", color=COLOR_OK), ephemeral=True
        )

    @discord.ui.button(label="คำสั่งทั้งหมด", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="olp:admin:help", row=2)
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(title="📖 คำสั่งทั้งหมด", description=HELP_TEXT, color=COLOR_MAIN), ephemeral=True
        )


class AdminPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg

    @app_commands.command(name="panel_admin", description="โพสต์เมนูแอดมิน (แผงปุ่มรวมงานแอดมิน)")
    async def panel_admin(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:admin:")

        embed = discord.Embed(
            title="🛠️ OLP-Noir · เมนูแอดมิน",
            description=(
                "กดปุ่มได้เลย ผลลัพธ์จะเห็นเฉพาะคนกด\n\n"
                "**ดูข้อมูล** — 📊 สรุปยอดรอบนี้ · 🕒 ชั่วโมงงาน · 🟢 ใครอยู่ในกะ\n"
                "**จัดการ** — 💎 ให้สิทธิ์ VIP · ✏️ แก้เวลาเข้างาน · ✂️ ตัดรอบทันที\n"
                "**ระบบ** — 🩺 สถานะระบบ · 🔄 โหลด config ใหม่ · 📖 คำสั่งทั้งหมด\n\n"
                "*ควรโพสต์ในห้องที่เห็นเฉพาะแอดมิน (คนอื่นกดก็ใช้ไม่ได้)*"
            ),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=AdminPanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์เมนูแอดมินแล้วค่ะ{note}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(AdminPanel())
    await bot.add_cog(AdminPanelCog(bot))

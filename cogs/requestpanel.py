"""Request Panel สำหรับลูกค้า: สอบถามเจ้าหน้าที่ / ซื้อ-ต่ออายุ VIP / ตรวจสอบสิทธิ์"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_MAIN
from core.utils import is_admin, purge_old_panels


class RequestPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="สอบถามเจ้าหน้าที่",
        emoji="💬",
        style=discord.ButtonStyle.primary,
        custom_id="olp:request:ticket",
    )
    async def ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("TicketsCog")
        await cog.open_ticket(interaction)

    @discord.ui.button(
        label="ซื้อ VIP / ต่ออายุ",
        emoji="💎",
        style=discord.ButtonStyle.success,
        custom_id="olp:request:vip",
    )
    async def vip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("VipCog")
        await cog.open_vip_shop(interaction)

    @discord.ui.button(
        label="ตรวจสอบสิทธิ์ VIP",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:request:vip_check",
    )
    async def vip_check(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("VipCog")
        await cog.check_vip(interaction)


class RequestPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg

    @app_commands.command(name="panel_request", description="โพสต์ Request Panel สำหรับลูกค้า")
    async def panel_request(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:request:")

        embed = discord.Embed(
            title="✨ OLP-Noir · บริการลูกค้า",
            description=(
                "เลือกรายการที่ต้องการได้เลยค่ะ ระบบจะติดต่อกลับทาง **DM** ของบอท\n\n"
                "💬 **สอบถามเจ้าหน้าที่** — คุยกับแอดมินแบบตัวต่อตัวผ่าน DM\n"
                "💎 **ซื้อ VIP / ต่ออายุ** — เลือกแพ็กเกจ ใส่โค้ดส่วนลด และชำระเงินได้เอง\n"
                "🔍 **ตรวจสอบสิทธิ์ VIP** — ดูแพ็กเกจและวันหมดอายุของคุณ\n\n"
                "*กรุณาเปิดรับข้อความ DM จากสมาชิกในเซิร์ฟเวอร์ก่อนใช้งานนะคะ*"
            ),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=RequestPanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์ Request Panel แล้วค่ะ{note}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(RequestPanel())
    await bot.add_cog(RequestPanelCog(bot))

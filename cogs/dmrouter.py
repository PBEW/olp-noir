"""ตัวจัดเส้นทางข้อความใน DM ของบอท (สลิป / สะพานแชท Ticket)"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.embeds import COLOR_INFO

log = logging.getLogger("olp.dm")


class DMRouterCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return

        has_image = any((a.content_type or "").startswith("image") for a in message.attachments)

        # 1) กำลังรอสลิป และส่งรูปเข้ามา
        pending = await self.db.get_pending_slip(message.author.id)
        if pending is not None and has_image:
            payments = self.bot.get_cog("PaymentsCog")
            await payments.receive_slip_image(message, pending)
            return

        # 2) สะพานแชท Ticket (ฝั่งลูกค้า หรือ ฝั่งแอดมิน)
        tickets = self.bot.get_cog("TicketsCog")
        ticket = await self.db.open_ticket_for_customer(message.author.id)
        if ticket is None:
            ticket = await self.db.active_ticket_for_admin(message.author.id)
        if ticket is not None and await tickets.relay(message, ticket):
            return

        # 3) ไม่มีบริบท — แนะนำวิธีใช้งาน
        if pending is not None:
            await message.reply(
                embed=discord.Embed(
                    title="📎 รอภาพสลิป",
                    description="กรุณาส่ง **ภาพสลิปโอนเงิน** เข้ามาใน DM นี้ แล้วกดปุ่มยืนยันค่ะ",
                    color=COLOR_INFO,
                )
            )
            return

        if message.content.strip():
            await message.reply(
                embed=discord.Embed(
                    title="🤖 OLP-Noir",
                    description=(
                        "ตอนนี้ยังไม่มีรายการที่กำลังดำเนินอยู่ค่ะ\n"
                        "กรุณาใช้ปุ่มที่หน้าแผงบริการในเซิร์ฟเวอร์เพื่อเริ่มรายการใหม่นะคะ"
                    ),
                    color=COLOR_INFO,
                )
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DMRouterCog(bot))

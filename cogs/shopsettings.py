"""⚙️ ตั้งค่าร้าน: แก้ config.json ผ่านเมนูในแผงแอดมิน (ห้อง, บริการ/ราคา, แพ็กเกจ VIP, ส่วนแบ่ง, โค้ดส่วนลด, การชำระเงิน, อื่น ๆ)

ทุกการบันทึกเขียนลง config.json ทันทีและมีผลเลยโดยไม่ต้องรีสตาร์ท
"""
from __future__ import annotations

import logging
import re
import time

import discord
from discord.ext import commands

from core.embeds import COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN, panel_embed, rows_text
from core.utils import is_admin, money

log = logging.getLogger("olp.settings")

NOT_ADMIN = "เฉพาะแอดมินเท่านั้นค่ะ"
TIERS = ("lace", "desire", "obsession")


# ---------------------------------------------------------------- helpers
def _cfg(interaction: discord.Interaction):
    return interaction.client.cfg


def _num(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(float(value), 2)


def format_price(entry) -> str:
    """ค่า pricing ใน config → ข้อความที่แอดมินอ่าน/แก้ได้"""
    if isinstance(entry, dict):
        if entry.get("unlimited"):
            return "ไม่จำกัด"
        return f"ฟรี{entry.get('free_per_month', 0)}+{_num(entry.get('after_price', 0))}"
    return str(_num(entry or 0))


def parse_price(text: str, normal: float):
    """ข้อความ → ค่า pricing: `99` · `ฟรี2` (ฟรี 2 ครั้ง/เดือน แล้วคิดราคาปกติ) · `ฟรี1+69` · `ไม่จำกัด` · ว่าง = ราคาปกติ"""
    t = text.strip().replace(",", "").replace("บาท", "").replace("฿", "").strip()
    if not t or t == "-":
        return _num(normal)
    if t in ("ไม่จำกัด", "unlimited"):
        return {"unlimited": True}
    m = re.fullmatch(r"(?:ฟรี|free)\s*(\d+)\s*(?:\+\s*(\d+(?:\.\d+)?))?", t, re.IGNORECASE)
    if m:
        after = float(m.group(2)) if m.group(2) else normal
        return {"free_per_month": int(m.group(1)), "after_price": _num(after)}
    try:
        value = float(t)
    except ValueError as exc:
        raise ValueError(f"อ่านราคา `{text}` ไม่ออก") from exc
    if value < 0:
        raise ValueError("ราคาติดลบไม่ได้")
    return _num(value)


def _parse_number(text: str, label: str, *, minimum: float = 0, maximum: float | None = None) -> float:
    try:
        value = float(text.strip().replace(",", "").replace("%", "").replace("บาท", ""))
    except ValueError as exc:
        raise ValueError(f"{label}: ต้องเป็นตัวเลข") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label}: ต้องอยู่ระหว่าง {minimum:g}–{maximum:g}" if maximum is not None else f"{label}: ต้องไม่น้อยกว่า {minimum:g}")
    return value


def _new_key(prefix: str) -> str:
    return f"{prefix}_{int(time.time())}"


async def _save(interaction: discord.Interaction, change: str) -> None:
    """บันทึก config.json แล้วแจ้งห้องแอดมินไว้เป็นประวัติ"""
    _cfg(interaction).save()
    log.info("ตั้งค่าร้าน: %s (โดย %s)", change, interaction.user)
    payments = interaction.client.get_cog("PaymentsCog")
    if payments is not None:
        await payments.notify_admin(
            embed=discord.Embed(description=f"⚙️ {change}\nโดย {interaction.user.mention}", color=COLOR_WARN),
            topic="log",
        )


class AdminOnlyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user, _cfg(interaction).admin_role_id):
            return True
        await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
        return False


class BackButton(discord.ui.Button):
    def __init__(self, row: int = 4) -> None:
        super().__init__(label="กลับ", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=home_embed(_cfg(interaction)), view=SettingsHome())


class SimpleModal(discord.ui.Modal):
    """Modal ทั่วไป: รับ dict ของช่อง แล้วเรียก on_done(interaction, values)"""

    def __init__(self, title: str, fields: list[dict], on_done) -> None:
        super().__init__(title=title[:45])
        self.on_done = on_done
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for f in fields:
            item = discord.ui.TextInput(
                label=f["label"][:45],
                default=str(f.get("default", ""))[:4000] or None,
                placeholder=(f.get("placeholder") or "")[:100] or None,
                required=f.get("required", True),
                max_length=f.get("max_length", 100),
                style=discord.TextStyle.paragraph if f.get("long") else discord.TextStyle.short,
            )
            self.inputs[f["name"]] = item
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, _cfg(interaction).admin_role_id):
            await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
            return
        values = {k: v.value.strip() for k, v in self.inputs.items()}
        try:
            await self.on_done(interaction, values)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)


# ------------------------------------------------------------------ home
SECTIONS = [
    ("bot", "ระบบบอท", "🧭", "ห้อง · Role (แอดมิน/รีเซปชั่น/พนักงาน/VIP) · เวลาแจ้งเตือน"),
    ("rooms", "ห้องบริการ", "🚪", "เพิ่ม / เปลี่ยนชื่อ / ลบห้อง"),
    ("services", "บริการ & ราคา", "🛎️", "เพิ่ม / แก้ราคา / ลบบริการ"),
    ("vip", "แพ็กเกจ VIP", "💎", "แก้ราคาแพ็กเกจ"),
    ("share", "ส่วนแบ่งพนักงาน", "💸", "% ค่าเริ่มต้น และรายคน"),
    ("discount", "โค้ดส่วนลด", "🎟️", "เพิ่ม / ลบโค้ด"),
    ("payment", "การชำระเงิน", "💳", "พร้อมเพย์ / ชื่อบัญชี / QR"),
    ("other", "อื่น ๆ", "🔧", "เวลาตัดออกงาน · โดเนท · Ticket"),
]


def home_embed(cfg) -> discord.Embed:
    missing = _missing_slots(cfg)
    pay = cfg.get("payment.promptpay", "") or ""
    pay_ok = bool(pay) and "X" not in pay.upper() and "x" not in pay
    return panel_embed(
        "⚙️ ตั้งค่าร้าน",
        "เลือกหมวดจากเมนูด้านล่าง แก้แล้วใช้ได้เลย ไม่ต้องรีสตาร์ท",
        [
            ("🧭 ระบบบอท", [
                (f"🧭 ห้อง · Role · เวลา · {'✅ ครบ' if not missing else f'⚠️ ขาด {missing}'}",
                 "ห้องแอดมิน/ตั๋ว/สลิป/เข้างาน · Role แอดมิน/รีเซปชั่น/พนักงาน/VIP · เวลาแจ้งเตือน"),
            ]),
            ("🏪 ร้าน & บริการ", [
                (f"🚪 ห้องบริการ · {len(cfg.rooms)} ห้อง", "ชื่อห้องที่ให้เลือกตอนเปิดบิล"),
                (f"🛎️ บริการ & ราคา · {len(cfg.services)} รายการ", "ราคาปกติ · ราคา VIP · เวลา · ต้องใช้ห้อง"),
                (f"💎 แพ็กเกจ VIP · {len(cfg.vip_packages)} แพ็กเกจ", "ราคาที่ลูกค้าซื้อเองใน Request Panel"),
            ]),
            ("💰 เงิน", [
                (f"💸 ส่วนแบ่งพนักงาน · {_num(cfg.get('revenue_share.default_staff_percent', 60))}%", "ค่าเริ่มต้น และตั้งรายคน"),
                (f"🎟️ โค้ดส่วนลด · {len(cfg.get('discount_codes', {}) or {})} โค้ด", "ใช้ตอนซื้อ VIP"),
                (f"💳 การชำระเงิน · {'✅ พร้อมใช้' if pay_ok else '⚠️ ยังไม่ใส่เลขพร้อมเพย์'}", "พร้อมเพย์ · ชื่อบัญชี · รูป QR"),
                ("🔧 อื่น ๆ", "เวลาตัดออกงาน · โดเนท · ปิด Ticket"),
            ]),
        ],
        footer="ทุกการแก้ไขแจ้งเข้าห้อง Log (ไม่ตั้ง = ห้องแอดมิน) ว่าใครแก้อะไร",
    )


class SettingsHome(AdminOnlyView):
    def __init__(self) -> None:
        super().__init__()
        self.section.options = [
            discord.SelectOption(label=label, value=key, emoji=emoji, description=desc)
            for key, label, emoji, desc in SECTIONS
        ]

    @discord.ui.select(placeholder="เลือกหมวดที่ต้องการแก้", row=0)
    async def section(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        view_cls = {
            "bot": BotSetupView,
            "rooms": RoomsView,
            "services": ServicesView,
            "vip": VipView,
            "share": ShareView,
            "discount": DiscountView,
            "payment": PaymentView,
            "other": OtherView,
        }[select.values[0]]
        view = view_cls(_cfg(interaction))
        await interaction.response.edit_message(embed=view.embed(), view=view)


class Section(AdminOnlyView):
    """หมวดตั้งค่า: มี select เลือกรายการ (ถ้ามี) + ปุ่มจัดการ + ปุ่มกลับ"""

    def __init__(self, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.selected: str | None = None
        self.add_item(BackButton())

    def embed(self) -> discord.Embed:  # override
        raise NotImplementedError

    async def refresh(self, interaction: discord.Interaction) -> None:
        view = type(self)(self.cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    def _picker(self, options: list[discord.SelectOption], placeholder: str) -> None:
        select = discord.ui.Select(
            placeholder=placeholder,
            options=options[:25] or [discord.SelectOption(label="(ยังไม่มีรายการ)", value="-")],
            row=0,
        )

        async def on_pick(interaction: discord.Interaction) -> None:
            self.selected = None if select.values[0] == "-" else select.values[0]
            for opt in select.options:
                opt.default = opt.value == select.values[0]
            await interaction.response.edit_message(view=self)

        select.callback = on_pick
        self.add_item(select)

    async def _need_selected(self, interaction: discord.Interaction) -> bool:
        if self.selected:
            return False
        await interaction.response.send_message("เลือกรายการจากเมนูก่อนค่ะ", ephemeral=True)
        return True


# ----------------------------------------------------------------- rooms
class RoomsView(Section):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._picker(
            [discord.SelectOption(label=r["name"][:100], value=r["key"]) for r in cfg.rooms], "เลือกห้องที่จะแก้/ลบ"
        )

    def embed(self) -> discord.Embed:
        rooms = "\n".join(f"{i}. {r['name']}" for i, r in enumerate(self.cfg.rooms, 1)) or "-"
        return discord.Embed(title="🚪 ห้องบริการ", description=rooms[:4000], color=COLOR_INFO)

    def _rooms(self) -> list[dict]:
        return self.cfg.data.setdefault("rooms", [])

    @discord.ui.button(label="เพิ่มห้อง", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def done(inter, v):
            self._rooms().append({"key": _new_key("room"), "name": v["name"]})
            await _save(inter, f"เพิ่มห้อง **{v['name']}**")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal("เพิ่มห้อง", [{"name": "name", "label": "ชื่อห้อง", "max_length": 80}], done)
        )

    @discord.ui.button(label="เปลี่ยนชื่อ", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._need_selected(interaction):
            return
        room = next(r for r in self._rooms() if r["key"] == self.selected)

        async def done(inter, v):
            old, room["name"] = room["name"], v["name"]
            await _save(inter, f"เปลี่ยนชื่อห้อง **{old}** → **{v['name']}**")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal("เปลี่ยนชื่อห้อง", [{"name": "name", "label": "ชื่อห้องใหม่", "default": room["name"], "max_length": 80}], done)
        )

    @discord.ui.button(label="ลบห้อง", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._need_selected(interaction):
            return
        room = next(r for r in self._rooms() if r["key"] == self.selected)
        self._rooms().remove(room)
        await _save(interaction, f"ลบห้อง **{room['name']}**")
        await self.refresh(interaction)


# -------------------------------------------------------------- services
def _service_fields(svc: dict | None) -> list[dict]:
    svc = svc or {}
    pricing = svc.get("pricing", {})
    vip_default = " / ".join(format_price(pricing.get(t, pricing.get("normal", 0))) for t in TIERS) if svc else ""
    return [
        {"name": "name", "label": "ชื่อบริการ", "default": svc.get("name", ""), "max_length": 80},
        {"name": "minutes", "label": "ระยะเวลา (นาที)", "default": svc.get("duration_minutes", 60), "max_length": 4},
        {"name": "normal", "label": "ราคาปกติ (บาท)", "default": format_price(pricing.get("normal", "")) if svc else "", "max_length": 8},
        {
            "name": "vip",
            "label": "ราคา VIP: Lace / Desire / Obsession",
            "default": vip_default,
            "placeholder": "เช่น 129 / 99 / ฟรี2+99  (ว่าง=ราคาปกติ, ไม่จำกัด=ฟรีตลอด)",
            "required": False,
            "max_length": 60,
        },
        {
            "name": "room",
            "label": "ต้องเลือกห้องไหม (ใช่ / ไม่)",
            "default": "ใช่" if svc.get("require_room") else "ไม่",
            "max_length": 5,
        },
    ]


def _apply_service(svc: dict, v: dict) -> None:
    normal = _parse_number(v["normal"], "ราคาปกติ")
    minutes = int(_parse_number(v["minutes"], "ระยะเวลา", minimum=1, maximum=1440))
    parts = [p for p in v["vip"].split("/")] if v["vip"] else []
    if len(parts) not in (0, 3):
        raise ValueError("ราคา VIP ต้องมี 3 ค่า คั่นด้วย / (Lace / Desire / Obsession)")
    pricing = {"normal": _num(normal)}
    for tier, text in zip(TIERS, parts or ["", "", ""]):
        pricing[tier] = parse_price(text, normal)
    svc["name"] = v["name"]
    svc["duration_minutes"] = minutes
    svc["require_room"] = v["room"].strip().lower() in ("ใช่", "yes", "y", "ต้อง", "true")
    svc["pricing"] = pricing


class ServicesView(Section):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._picker(
            [
                discord.SelectOption(
                    label=s["name"][:100], value=s["key"], emoji=s.get("emoji") or None,
                    description=f"ปกติ {format_price(s.get('pricing', {}).get('normal', 0))} บาท · {s.get('duration_minutes', 60)} นาที",
                )
                for s in cfg.services
            ],
            "เลือกบริการที่จะแก้/ลบ",
        )

    def embed(self) -> discord.Embed:
        lines = []
        for s in self.cfg.services:
            p = s.get("pricing", {})
            vip = " / ".join(format_price(p.get(t, p.get("normal", 0))) for t in TIERS)
            room = " · 🚪ต้องใช้ห้อง" if s.get("require_room") else ""
            lines.append(f"{s.get('emoji', '•')} **{s['name']}** — {format_price(p.get('normal', 0))} บาท / {s.get('duration_minutes', 60)} นาที\n　VIP: {vip}{room}")
        embed = discord.Embed(title="🛎️ บริการ & ราคา", description="\n".join(lines)[:4000] or "-", color=COLOR_INFO)
        embed.set_footer(text="รูปแบบราคา VIP: 99 = ราคาคงที่ · ฟรี2 = ฟรี 2 ครั้ง/เดือนแล้วคิดราคาปกติ · ฟรี1+69 = ฟรี 1 ครั้งแล้วคิด 69 · ไม่จำกัด")
        return embed

    @discord.ui.button(label="เพิ่มบริการ", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def done(inter, v):
            svc = {"key": _new_key("svc"), "emoji": "✨"}
            _apply_service(svc, v)
            self.cfg.data.setdefault("services", []).append(svc)
            await _save(inter, f"เพิ่มบริการ **{svc['name']}** ({format_price(svc['pricing']['normal'])} บาท)")
            await self.refresh(inter)

        await interaction.response.send_modal(SimpleModal("เพิ่มบริการ", _service_fields(None), done))

    @discord.ui.button(label="แก้ไข / ราคา", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._need_selected(interaction):
            return
        svc = self.cfg.service(self.selected)

        async def done(inter, v):
            _apply_service(svc, v)
            await _save(inter, f"แก้บริการ **{svc['name']}** — ปกติ {format_price(svc['pricing']['normal'])} บาท")
            await self.refresh(inter)

        await interaction.response.send_modal(SimpleModal("แก้บริการ", _service_fields(svc), done))

    @discord.ui.button(label="ลบบริการ", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._need_selected(interaction):
            return
        svc = self.cfg.service(self.selected)
        self.cfg.data["services"].remove(svc)
        await _save(interaction, f"ลบบริการ **{svc['name']}** (บิลเก่ายังเก็บไว้ตามเดิม)")
        await self.refresh(interaction)


# ------------------------------------------------------------------- VIP
class VipView(Section):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._picker(
            [
                discord.SelectOption(label=p["name"][:100], value=p["key"], emoji=p.get("emoji") or None, description=money(p["price"]))
                for p in cfg.vip_packages
            ],
            "เลือกแพ็กเกจที่จะแก้ราคา",
        )

    def embed(self) -> discord.Embed:
        lines = [f"{p.get('emoji', '')} **{p['name']}** — {money(p['price'])}" for p in self.cfg.vip_packages]
        return discord.Embed(title="💎 แพ็กเกจ VIP", description="\n".join(lines) or "-", color=COLOR_INFO)

    @discord.ui.button(label="แก้ราคา", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._need_selected(interaction):
            return
        pkg = self.cfg.vip_package(self.selected)

        async def done(inter, v):
            old = pkg["price"]
            pkg["price"] = _num(_parse_number(v["price"], "ราคา"))
            await _save(inter, f"แก้ราคา **{pkg['name']}** {money(old)} → {money(pkg['price'])}")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal(pkg["name"], [{"name": "price", "label": "ราคา (บาท)", "default": _num(pkg["price"]), "max_length": 8}], done)
        )


# ----------------------------------------------------------------- share
class ShareView(Section):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.member_id: int | None = None
        select = discord.ui.UserSelect(placeholder="เลือกพนักงาน (สำหรับตั้ง % รายคน)", row=0)

        async def on_pick(interaction: discord.Interaction) -> None:
            self.member_id = select.values[0].id
            await interaction.response.defer()

        select.callback = on_pick
        self.add_item(select)

    def _share(self) -> dict:
        return self.cfg.data.setdefault("revenue_share", {})

    def embed(self) -> discord.Embed:
        share = self._share()
        lines = [f"<@{uid}> — **{_num(pct)}%**" for uid, pct in (share.get("staff_percent") or {}).items()]
        embed = discord.Embed(title="💸 ส่วนแบ่งพนักงาน", color=COLOR_INFO)
        embed.add_field(name="ค่าเริ่มต้น (ทุกคน)", value=f"**{_num(share.get('default_staff_percent', 60))}%** ของยอดบิล", inline=False)
        embed.add_field(name="ตั้งรายคน", value="\n".join(lines)[:1024] or "-", inline=False)
        embed.set_footer(text="มีผลกับบิลที่เปิดใหม่หลังจากนี้ บิลเก่าไม่เปลี่ยน")
        return embed

    @discord.ui.button(label="แก้ % ค่าเริ่มต้น", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def default(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        share = self._share()

        async def done(inter, v):
            share["default_staff_percent"] = _num(_parse_number(v["pct"], "เปอร์เซ็นต์", maximum=100))
            await _save(inter, f"ส่วนแบ่งพนักงานค่าเริ่มต้น → **{share['default_staff_percent']}%**")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal("ส่วนแบ่งค่าเริ่มต้น", [{"name": "pct", "label": "% ที่พนักงานได้", "default": _num(share.get("default_staff_percent", 60)), "max_length": 5}], done)
        )

    @discord.ui.button(label="ตั้ง % รายคน", emoji="👤", style=discord.ButtonStyle.primary, row=1)
    async def per_staff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.member_id is None:
            await interaction.response.send_message("เลือกพนักงานจากเมนูก่อนค่ะ", ephemeral=True)
            return
        table = self._share().setdefault("staff_percent", {})
        uid = str(self.member_id)

        async def done(inter, v):
            if not v["pct"]:
                table.pop(uid, None)
                await _save(inter, f"ลบส่วนแบ่งรายคนของ <@{uid}> (กลับไปใช้ค่าเริ่มต้น)")
            else:
                table[uid] = _num(_parse_number(v["pct"], "เปอร์เซ็นต์", maximum=100))
                await _save(inter, f"ส่วนแบ่งของ <@{uid}> → **{table[uid]}%**")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal("ส่วนแบ่งรายคน", [{"name": "pct", "label": "% ที่ได้ (เว้นว่าง = ใช้ค่าเริ่มต้น)", "default": table.get(uid, ""), "required": False, "max_length": 5}], done)
        )


# -------------------------------------------------------------- discount
class DiscountView(Section):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._picker(
            [discord.SelectOption(label=code, value=code, description=(d.get("note") or "")[:100] or None) for code, d in self._codes().items()],
            "เลือกโค้ดที่จะลบ",
        )

    def _codes(self) -> dict:
        return self.cfg.data.setdefault("discount_codes", {})

    def embed(self) -> discord.Embed:
        lines = [
            f"`{code}` — ลด {_num(d['value'])}{'%' if d['type'] == 'percent' else ' บาท'}" + (f" · {d['note']}" if d.get("note") else "")
            for code, d in self._codes().items()
        ]
        return discord.Embed(title="🎟️ โค้ดส่วนลด (ใช้กับการซื้อ VIP)", description="\n".join(lines)[:4000] or "-", color=COLOR_INFO)

    @discord.ui.button(label="เพิ่ม / แก้โค้ด", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def done(inter, v):
            code = re.sub(r"\s+", "", v["code"]).upper()
            if not code:
                raise ValueError("ต้องใส่โค้ด")
            kind = "percent" if v["type"].strip() in ("%", "เปอร์เซ็นต์", "percent") else "amount"
            value = _parse_number(v["value"], "มูลค่า", minimum=0.01, maximum=100 if kind == "percent" else None)
            self._codes()[code] = {"type": kind, "value": _num(value), "note": v["note"]}
            await _save(inter, f"บันทึกโค้ดส่วนลด `{code}` ลด {_num(value)}{'%' if kind == 'percent' else ' บาท'}")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal(
                "โค้ดส่วนลด",
                [
                    {"name": "code", "label": "โค้ด (ภาษาอังกฤษ/ตัวเลข)", "default": self.selected or "", "max_length": 30},
                    {"name": "type", "label": "ลดแบบ: % หรือ บาท", "default": "%", "max_length": 10},
                    {"name": "value", "label": "มูลค่า", "max_length": 8},
                    {"name": "note", "label": "หมายเหตุ", "required": False, "max_length": 100},
                ],
                done,
            )
        )

    @discord.ui.button(label="ลบโค้ด", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._need_selected(interaction):
            return
        self._codes().pop(self.selected, None)
        await _save(interaction, f"ลบโค้ดส่วนลด `{self.selected}`")
        await self.refresh(interaction)


# --------------------------------------------------------------- payment
class PaymentView(Section):
    def _pay(self) -> dict:
        return self.cfg.data.setdefault("payment", {})

    def embed(self) -> discord.Embed:
        p = self._pay()
        embed = discord.Embed(title="💳 การชำระเงิน", color=COLOR_INFO)
        embed.add_field(name="ชื่อบัญชี", value=p.get("account_name") or "-", inline=True)
        embed.add_field(name="พร้อมเพย์", value=p.get("promptpay") or "-", inline=True)
        embed.add_field(name="ข้อความท้าย", value=p.get("note") or "-", inline=False)
        qr = p.get("qr_image_url") or ""
        if qr.startswith(("http://", "https://")):
            embed.set_thumbnail(url=qr)
            embed.add_field(name="รูป QR", value="✅ ตั้งค่าแล้ว (ลิงก์จาก Discord จะหมดอายุ แนะนำ imgur)", inline=False)
        else:
            embed.add_field(name="รูป QR", value="⚠️ ยังไม่ได้ตั้ง", inline=False)
        return embed

    @discord.ui.button(label="แก้ไข", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        p = self._pay()

        async def done(inter, v):
            p.update(account_name=v["account"], promptpay=v["promptpay"], qr_image_url=v["qr"], note=v["note"])
            await _save(inter, "แก้ข้อมูลการชำระเงิน")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal(
                "การชำระเงิน",
                [
                    {"name": "account", "label": "ชื่อบัญชี", "default": p.get("account_name", ""), "max_length": 100},
                    {"name": "promptpay", "label": "เลขพร้อมเพย์", "default": p.get("promptpay", ""), "max_length": 30},
                    {"name": "qr", "label": "ลิงก์รูป QR (ไม่ใส่ก็ได้)", "default": p.get("qr_image_url", ""), "required": False, "max_length": 500},
                    {"name": "note", "label": "ข้อความท้ายยอดชำระ", "default": p.get("note", ""), "required": False, "max_length": 200, "long": True},
                ],
                done,
            )
        )


# ----------------------------------------------------------------- other
class OtherView(Section):
    def embed(self) -> discord.Embed:
        c = self.cfg
        embed = discord.Embed(title="🔧 อื่น ๆ", color=COLOR_INFO)
        embed.add_field(name="เวลาตัดออกงานอัตโนมัติ", value=f"{c.attendance_cutoff_hour:02d}:{c.attendance_cutoff_minute:02d} น.", inline=True)
        embed.add_field(name="โดเนทขั้นต่ำ", value=money(c.get("donate.min_amount", 20)), inline=True)
        embed.add_field(name="% โดเนทที่โฮสต์ได้", value=f"{_num(c.get('donate.staff_percent', 100))}%", inline=True)
        embed.add_field(name="ปิด Ticket เมื่อเงียบเกิน", value=f"{c.ticket_timeout_minutes} นาที", inline=True)
        return embed

    @discord.ui.button(label="แก้ไข", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        c = self.cfg

        async def done(inter, v):
            m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", v["cutoff"])
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ValueError("เวลาตัดออกงานต้องเป็นรูปแบบ HH:MM เช่น 01:00")
            att = c.data.setdefault("attendance", {})
            att["cutoff_hour"], att["cutoff_minute"] = int(m.group(1)), int(m.group(2))
            don = c.data.setdefault("donate", {})
            don["min_amount"] = _num(_parse_number(v["min"], "โดเนทขั้นต่ำ", minimum=1))
            don["staff_percent"] = _num(_parse_number(v["pct"], "% โดเนท", maximum=100))
            c.data.setdefault("ticket", {})["timeout_minutes"] = int(_parse_number(v["ticket"], "Ticket", minimum=1, maximum=1440))
            await _save(inter, "แก้ค่าอื่น ๆ (เวลาตัดออกงาน / โดเนท / Ticket)")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal(
                "อื่น ๆ",
                [
                    {"name": "cutoff", "label": "เวลาตัดออกงานอัตโนมัติ (HH:MM)", "default": f"{c.attendance_cutoff_hour:02d}:{c.attendance_cutoff_minute:02d}", "max_length": 5},
                    {"name": "min", "label": "โดเนทขั้นต่ำ (บาท)", "default": _num(c.get("donate.min_amount", 20)), "max_length": 8},
                    {"name": "pct", "label": "% ที่โฮสต์ได้เมื่อโดเนทให้โฮสต์", "default": _num(c.get("donate.staff_percent", 100)), "max_length": 5},
                    {"name": "ticket", "label": "ปิด Ticket เมื่อเงียบเกิน (นาที)", "default": c.ticket_timeout_minutes, "max_length": 4},
                ],
                done,
            )
        )


# ============================================================== ระบบบอท
# (key, ชนิด, ชื่อ, path ใน config, เลือกได้หลายอัน, คำอธิบาย)
BOT_SLOTS = [
    ("ch_admin", "channel", "🛠️ ห้องแอดมิน", "channels.admin", False, "สรุปยอด · ตัดรอบ · แจ้งเตือนทั่วไป (ห้องหลัก)"),
    ("ch_ticket", "channel", "💬 ห้องรับเรื่องลูกค้า", "channels.ticket", False, "ลูกค้าทัก DM ใหม่ → ปุ่มรับเรื่อง"),
    ("ch_slip", "channel", "🧾 ห้องตรวจสลิป", "channels.slip", False, "สลิปรอยืนยัน ✅/❌ · สลิปค้าง · ส่ง DM ยอดไม่สำเร็จ"),
    ("ch_attendance", "channel", "🕒 ห้องเข้างาน", "channels.attendance", False, "พนักงานเข้างาน · สรุปคนมาทำงานตอนตี 1"),
    ("ch_review", "channel", "💖 ห้องรีวิว", "channels.review", False, "รีวิวที่อนุมัติแล้ว"),
    ("ch_donate", "channel", "🎁 ห้องประกาศโดเนท", "channels.donate", False, "ประกาศขอบคุณผู้โดเนท"),
    ("ch_log", "channel", "📝 ห้อง Log", "channels.log", False, "ประวัติการแก้ตั้งค่าร้าน"),
    ("role_admin", "role", "🛠️ Role แอดมิน", "roles.admin", False, "ใช้ทุกเมนู · ตั้งค่าร้าน"),
    ("role_reception", "role", "🔑 Role รีเซปชั่น", "roles.reception", True, "แผงรีเซปชั่น · ตรวจสลิป · รับเรื่องลูกค้า"),
    ("role_staff", "role", "💃 Role พนักงาน", "roles.staff", True, "เมนูพนักงาน · เข้างาน · รายชื่อตอนเปิดบิล"),
    ("role_on_duty", "role", "🟢 Role On Duty", "roles.on_duty", False, "บอทให้ตอนเข้างาน ถอดตอนตี 1"),
    ("vip_lace", "role", "💎 Role VIP Lace", "vip:lace", False, "Role ที่บอทให้เมื่อซื้อ VIP Lace"),
    ("vip_desire", "role", "🔥 Role VIP Desire", "vip:desire", False, "Role ที่บอทให้เมื่อซื้อ VIP Desire"),
    ("vip_obsession", "role", "👑 Role VIP Obsession", "vip:obsession", False, "Role ระดับสูงสุด"),
]
# ไม่ตั้งก็ได้ (ส่งเข้าห้องแอดมินแทน / ไม่ใช้ฟีเจอร์นั้น)
OPTIONAL_SLOTS = {"channels.ticket", "channels.slip", "channels.attendance", "channels.log", "channels.donate", "roles.on_duty", "roles.reception"}
FALLBACK_TO_ADMIN = {"channels.ticket", "channels.slip", "channels.attendance", "channels.log"}


def _slot_ids(cfg, path: str) -> list[int]:
    if path.startswith("vip:"):
        tier = cfg.vip_tier(path[4:]) or {}
        raw = tier.get("role_id", 0)
    else:
        raw = cfg.get(path, 0) or 0
    values = raw if isinstance(raw, list) else [raw]
    return [int(v) for v in values if str(v).strip().isdigit() and int(v)]


def _set_slot(cfg, path: str, value) -> None:
    if path.startswith("vip:"):
        for tier in cfg.data.get("vip_tiers", []):
            if tier.get("key") == path[4:]:
                tier["role_id"] = value
        return
    section, key = path.split(".")
    cfg.data.setdefault(section, {})[key] = value


def _slot_text(kind: str, ids: list[int], path: str) -> str:
    if not ids:
        if path in FALLBACK_TO_ADMIN:
            return "↪️ ใช้ห้องแอดมิน"
        return "▫️ ไม่ใช้" if path in OPTIONAL_SLOTS else "⚠️ ยังไม่ตั้ง"
    return " ".join(f"<#{i}>" if kind == "channel" else f"<@&{i}>" for i in ids)


def _missing_slots(cfg) -> int:
    return sum(1 for _, _, _, p, _, _ in BOT_SLOTS if not _slot_ids(cfg, p) and p not in OPTIONAL_SLOTS)


class BotSetupView(Section):
    """ตั้งห้อง / Role ที่บอทต้องใช้ + เวลาแจ้งเตือน (ไม่ต้องแก้ config.json เอง)"""

    def __init__(self, cfg, slot: str | None = None) -> None:
        super().__init__(cfg)
        self.slot = slot
        select = discord.ui.Select(
            placeholder="1) เลือกห้อง / Role ที่จะตั้ง",
            row=0,
            options=[
                discord.SelectOption(
                    label=name.split(" ", 1)[1][:100],
                    value=key,
                    emoji=name.split(" ", 1)[0],
                    description=(("✅ " if _slot_ids(cfg, path) else "▫️ " if path in OPTIONAL_SLOTS else "⚠️ ") + detail)[:100],
                    default=key == slot,
                )
                for key, _, name, path, _, detail in BOT_SLOTS
            ],
        )

        async def on_slot(interaction: discord.Interaction) -> None:
            await self._rerender(interaction, select.values[0])

        select.callback = on_slot
        self.add_item(select)

        spec = self._spec()
        if spec is not None:
            _, kind, name, _, multi, _ = spec
            if kind == "channel":
                value_select = discord.ui.ChannelSelect(
                    placeholder=f"2) เลือก{name.split(' ', 1)[1]}",
                    channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                    row=1,
                )
            else:
                value_select = discord.ui.RoleSelect(
                    placeholder=f"2) เลือก{name.split(' ', 1)[1]}" + (" (เลือกได้หลายอัน)" if multi else ""),
                    max_values=10 if multi else 1,
                    row=1,
                )

            async def on_value(interaction: discord.Interaction) -> None:
                await self.apply_ids(interaction, [int(v.id) for v in value_select.values])

            value_select.callback = on_value
            self.add_item(value_select)
        else:
            self.id_button.disabled = True
            self.clear_slot.disabled = True

    def _spec(self):
        return next((s for s in BOT_SLOTS if s[0] == self.slot), None)

    def embed(self) -> discord.Embed:
        cfg = self.cfg

        def rows(kind):
            return rows_text(
                [(n.split(" ", 1)[0], n.split(" ", 1)[1], _slot_text(k, _slot_ids(cfg, p), p)) for _, k, n, p, _, _ in BOT_SLOTS if k == kind]
            )

        missing = _missing_slots(cfg)
        embed = discord.Embed(
            title="🧭 ตั้งค่าระบบบอท",
            description=(
                "1️⃣ เลือกห้อง/Role จากเมนูแรก　2️⃣ เลือกจากเมนูที่สอง (หรือกด ✏️ ใส่ ID) บันทึกให้เลย\n"
                + (f"⚠️ ยังไม่ได้ตั้ง **{missing}** รายการที่จำเป็น" if missing else "✅ ตั้งห้องและ Role ที่จำเป็นครบแล้ว")
            ),
            color=COLOR_OK if not missing else COLOR_WARN,
        )
        embed.add_field(name="📍 ห้อง", value=rows("channel")[:1024], inline=False)
        embed.add_field(name="🏷️ Role", value=rows("role")[:1024], inline=False)
        embed.add_field(
            name="⏰ เวลา & แจ้งเตือน",
            value=rows_text([
                ("🔔", "เตือนก่อนเริ่มงาน", f"{cfg.before_start_minutes} นาที"),
                ("⌛", "เตือนก่อนหมดเวลา", f"{cfg.before_end_minutes} นาที"),
                ("🎫", "ปิดตั๋วเมื่อเงียบ", f"{cfg.ticket_timeout_minutes} นาที"),
                ("💖", "เขียนรีวิวได้ภายใน", f"{cfg.review_window_hours} ชม."),
                ("🔎", "แจ้งสลิปค้างตรวจ", f"{cfg.slip_review_minutes} นาที"),
            ]),
            inline=False,
        )
        embed.set_footer(text="Token / Guild ID / credentials.json ยังต้องแก้ในไฟล์ (ต้องรีสตาร์ท)")
        return embed

    async def _rerender(self, interaction: discord.Interaction, slot: str | None) -> None:
        view = BotSetupView(self.cfg, slot)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def refresh(self, interaction: discord.Interaction) -> None:
        await self._rerender(interaction, self.slot)

    async def apply_ids(self, interaction: discord.Interaction, ids: list[int]) -> None:
        key, kind, name, path, multi, _ = self._spec()
        if path == "roles.admin":
            member = interaction.user
            perms = getattr(member, "guild_permissions", None)
            has_role = any(r.id == ids[0] for r in getattr(member, "roles", []))
            if not has_role and not (perms and (perms.administrator or perms.manage_guild)):
                await interaction.response.send_message(
                    "⚠️ ตั้ง Role แอดมินเป็น Role ที่คุณไม่มีไม่ได้ (จะล็อกตัวเองออกจากเมนูแอดมิน)", ephemeral=True
                )
                return
        _set_slot(self.cfg, path, ids if multi else ids[0])
        await _save(interaction, f"ตั้ง{name}: {_slot_text(kind, ids, path)}")
        await self._rerender(interaction, key)

    @discord.ui.button(label="ใส่ ID", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def id_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _, kind, name, path, multi, _ = self._spec()

        async def done(inter, v):
            parts = [p.strip().strip("<>#@&") for p in v["ids"].replace(" ", ",").split(",") if p.strip()]
            if not parts or any(not p.isdigit() for p in parts):
                raise ValueError("ID ต้องเป็นตัวเลขเท่านั้น")
            if not multi and len(parts) > 1:
                raise ValueError("ช่องนี้ใส่ได้ ID เดียว")
            ids = list(dict.fromkeys(int(p) for p in parts))
            guild = inter.guild
            if kind == "channel":
                bad = [i for i in ids if guild is None or not hasattr(guild.get_channel(i), "send")]
            else:
                bad = [i for i in ids if guild is None or guild.get_role(i) is None]
            if bad:
                raise ValueError("ไม่พบในเซิร์ฟเวอร์นี้: " + ", ".join(map(str, bad)))
            await self.apply_ids(inter, ids)

        await interaction.response.send_modal(
            SimpleModal(
                name.split(" ", 1)[1],
                [{
                    "name": "ids",
                    "label": ("Channel ID" if kind == "channel" else "Role ID") + (" (หลายอันคั่นด้วย ,)" if multi else ""),
                    "default": ", ".join(map(str, _slot_ids(self.cfg, path))),
                    "placeholder": "คลิกขวา → Copy ID เช่น 1234567890123456789",
                    "max_length": 200,
                }],
                done,
            )
        )

    @discord.ui.button(label="ล้างค่า", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def clear_slot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        key, kind, name, path, multi, _ = self._spec()
        if path == "roles.admin":
            await interaction.response.send_message("ล้าง Role แอดมินไม่ได้ค่ะ (เลือก Role ใหม่แทน)", ephemeral=True)
            return
        _set_slot(self.cfg, path, [] if multi else 0)
        await _save(interaction, f"ล้างค่า{name}")
        await self._rerender(interaction, key)

    @discord.ui.button(label="เวลา & แจ้งเตือน", emoji="⏰", style=discord.ButtonStyle.secondary, row=3)
    async def times(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        c = self.cfg

        async def done(inter, v):
            notify = c.data.setdefault("notify", {})
            notify["before_start_minutes"] = int(_parse_number(v["start"], "เตือนก่อนเริ่ม", maximum=180))
            notify["before_end_minutes"] = int(_parse_number(v["end"], "เตือนก่อนหมด", maximum=180))
            c.data.setdefault("ticket", {})["timeout_minutes"] = int(_parse_number(v["ticket"], "ปิดตั๋ว", minimum=1, maximum=1440))
            c.data.setdefault("review", {})["window_hours"] = int(_parse_number(v["review"], "รีวิว", minimum=1, maximum=720))
            c.data.setdefault("bill_timeout", {})["slip_review_minutes"] = int(_parse_number(v["slip"], "สลิปค้าง", minimum=1, maximum=1440))
            await _save(inter, "แก้เวลา & แจ้งเตือน")
            await self.refresh(inter)

        await interaction.response.send_modal(
            SimpleModal(
                "⏰ เวลา & แจ้งเตือน",
                [
                    {"name": "start", "label": "เตือนก่อนเริ่มงาน (นาที)", "default": c.before_start_minutes, "max_length": 4},
                    {"name": "end", "label": "เตือนก่อนหมดเวลา (นาที)", "default": c.before_end_minutes, "max_length": 4},
                    {"name": "ticket", "label": "ปิดตั๋วเมื่อเงียบเกิน (นาที)", "default": c.ticket_timeout_minutes, "max_length": 4},
                    {"name": "review", "label": "เขียนรีวิวได้ภายใน (ชั่วโมง)", "default": c.review_window_hours, "max_length": 4},
                    {"name": "slip", "label": "แจ้งสลิปค้างตรวจเมื่อเกิน (นาที)", "default": c.slip_review_minutes, "max_length": 4},
                ],
                done,
            )
        )


class ShopSettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def open_settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=home_embed(self.bot.cfg), view=SettingsHome(), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ShopSettingsCog(bot))

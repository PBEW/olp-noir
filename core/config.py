"""โหลด/เข้าถึงค่าตั้งค่าของบอทจากไฟล์ config.json"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            example = self.path.with_name("config.example.json")
            if example.exists():
                shutil.copy(example, self.path)
            else:
                raise ConfigError(f"ไม่พบไฟล์ config: {self.path}")
        self.reload()

    # ------------------------------------------------------------------ base
    def reload(self) -> None:
        with self.path.open(encoding="utf-8") as fp:
            self.data: dict[str, Any] = json.load(fp)

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as fp:
            json.dump(self.data, fp, ensure_ascii=False, indent=2)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # --------------------------------------------------------------- general
    @property
    def guild_id(self) -> int:
        return int(self.get("guild_id", 0) or 0)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.get("timezone", "Asia/Bangkok"))

    def channel_id(self, name: str) -> int:
        return int(self.get(f"channels.{name}", 0) or 0)

    @property
    def admin_role_id(self) -> int:
        return int(self.get("roles.admin", 0) or 0)

    @property
    def staff_role_id(self) -> int:
        return int(self.get("roles.staff", 0) or 0)

    # -------------------------------------------------------------- services
    @property
    def services(self) -> list[dict]:
        return list(self.get("services", []) or [])

    def bookable_services(self) -> list[dict]:
        return [s for s in self.services if not s.get("extend_only")]

    def extend_services(self) -> list[dict]:
        return [s for s in self.services if s.get("extend_only")]

    def service(self, key: str) -> dict | None:
        return next((s for s in self.services if s["key"] == key), None)

    def service_name(self, key: str) -> str:
        svc = self.service(key)
        return svc["name"] if svc else key

    def service_names(self, keys: list[str]) -> str:
        return ", ".join(self.service_name(k) for k in keys) or "-"

    def service_pricing_entry(self, service: dict, tier_key: str | None) -> float | dict:
        """ค่า pricing ดิบของบริการตามระดับ VIP (ตัวเลข = ราคาคงที่, dict = มีเงื่อนไขโควต้า)"""
        pricing = service.get("pricing", {})
        key = tier_key or "normal"
        return pricing.get(key, pricing.get("normal", 0))

    @property
    def rooms(self) -> list[dict]:
        return list(self.get("rooms", []) or [])

    def room_name(self, key: str | None) -> str:
        if not key:
            return "-"
        room = next((r for r in self.rooms if r["key"] == key), None)
        return room["name"] if room else key

    # ------------------------------------------------------------------- VIP
    @property
    def vip_tiers(self) -> list[dict]:
        """เรียงจากระดับต่ำไปสูงตาม rank เสมอ"""
        return sorted(self.get("vip_tiers", []) or [], key=lambda t: t.get("rank", 0))

    def vip_tier(self, key: str | None) -> dict | None:
        if not key:
            return None
        return next((t for t in self.vip_tiers if t["key"] == key), None)

    def vip_tier_rank(self, key: str | None) -> int:
        tier = self.vip_tier(key)
        return int(tier["rank"]) if tier else 0

    def vip_tier_name(self, key: str | None) -> str:
        tier = self.vip_tier(key)
        return tier["name"] if tier else "ลูกค้าทั่วไป"

    def purchasable_vip_tiers(self) -> list[dict]:
        return [t for t in self.vip_tiers if t.get("purchasable", True)]

    @property
    def vip_role_ids(self) -> list[int]:
        return [int(t["role_id"]) for t in self.vip_tiers if int(t.get("role_id") or 0)]

    @property
    def vip_packages(self) -> list[dict]:
        return list(self.get("vip_packages", []) or [])

    def vip_package(self, key: str) -> dict | None:
        return next((p for p in self.vip_packages if p["key"] == key), None)

    def vip_packages_for_tier(self, tier_key: str) -> list[dict]:
        return [p for p in self.vip_packages if p["tier"] == tier_key]

    def discount(self, code: str) -> dict | None:
        codes = self.get("discount_codes", {}) or {}
        return codes.get(code.strip().upper())

    # ---------------------------------------------------------------- shares
    def staff_percent(self, staff_id: int) -> float:
        table = self.get("revenue_share.staff_percent", {}) or {}
        if str(staff_id) in table:
            return float(table[str(staff_id)])
        return float(self.get("revenue_share.default_staff_percent", 60))

    # --------------------------------------------------------------- numbers
    @property
    def loop_seconds(self) -> int:
        return int(self.get("notify.loop_seconds", 35))

    @property
    def before_start_minutes(self) -> int:
        return int(self.get("notify.before_start_minutes", 5))

    @property
    def before_end_minutes(self) -> int:
        return int(self.get("notify.before_end_minutes", 5))

    @property
    def ticket_timeout_minutes(self) -> int:
        return int(self.get("ticket.timeout_minutes", 10))

    @property
    def review_window_hours(self) -> int:
        return int(self.get("review.window_hours", 24))

    @property
    def review_color(self) -> int:
        raw = str(self.get("review.embed_color", "#FF6FA5")).lstrip("#")
        try:
            return int(raw, 16)
        except ValueError:
            return 0xFF6FA5

    @property
    def cutoff_weekday(self) -> int:
        """0 = จันทร์ ... 5 = เสาร์ (ตาม datetime.weekday())"""
        return int(self.get("cutoff.weekday", 5))

    @property
    def cutoff_hour(self) -> int:
        return int(self.get("cutoff.hour", 8))

    @property
    def cutoff_minute(self) -> int:
        return int(self.get("cutoff.minute", 0))

"""บันทึกบัญชีลง Google Sheets (ทำงานแบบ optional — ปิดได้ใน config)

ชีตรอบบิล (Cycle_YYYY-MM-DD): ตาราง A:P + กล่องสรุปด้านขวา Q:S
ชีต Attendance: บันทึกเข้างานรายวัน · ชีต Donations: โดเนท
ทุกชีตจัดรูปแบบให้อ่านง่าย (หัวตารางสีดำ-ทอง, แยกสีคอลัมน์เงิน, ไฮไลต์บิลต่อเวลา, ปุ่มตัวกรอง)
สั่งจัดรูปแบบใหม่ได้ด้วย /sheets_format
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("olp.sheets")

# ลำดับคอลัมน์ต้องตรงกับแถวที่ PaymentsCog.log_job_to_sheet เขียน (A..P)
HEADERS = [
    "เลขบิล",                   # A
    "วันที่",                    # B
    "เริ่ม",                     # C
    "จบ",                       # D
    "ลูกค้า",                    # E
    "ID ลูกค้า",                 # F (ซ่อน)
    "พนักงาน",                  # G
    "ID พนักงาน",               # H (ซ่อน)
    "บริการ",                    # I
    "ห้อง",                      # J
    "💰 ยอดบิล (In)",            # K
    "💃 ส่วนแบ่งพนักงาน (Out)",   # L
    "🏠 รายได้ร้าน",              # M
    "ประเภท",                   # N
    "ระดับ VIP",                 # O
    "หมายเหตุ",                 # P
]

# กล่องสรุปด้านขวา (คอลัมน์ Q:S)
SUMMARY_BLOCK = [
    ["📊 สรุปรอบนี้", "", ""],
    ["💰 ยอดบิลรวม (In)", "=SUM(K2:K)", ""],
    ["💃 จ่ายพนักงาน (Out)", "=SUM(L2:L)", ""],
    ["🏠 รายได้เข้าร้าน", "=SUM(M2:M)", ""],
    ["🧾 จำนวนบิล", "=COUNTA(A2:A)", ""],
    ["", "", ""],
    ["พนักงาน", "ยอดบิล", "ส่วนแบ่ง (ต้องโอน)"],
    [
        "=IFERROR(UNIQUE(FILTER(G2:G,G2:G<>\"\")),\"\")",
        "=ARRAYFORMULA(IF(Q8:Q=\"\",,SUMIF(G:G,Q8:Q,K:K)))",
        "=ARRAYFORMULA(IF(Q8:Q=\"\",,SUMIF(G:G,Q8:Q,L:L)))",
    ],
]

ATTENDANCE_SHEET = "Attendance"
ATTENDANCE_HEADERS = [
    "เลขที่",                    # A
    "วันที่",                    # B
    "เข้างาน",                   # C
    "ออกงาน (ตัดอัตโนมัติ)",      # D
    "พนักงาน",                  # E
    "ID พนักงาน",               # F (ซ่อน)
    "ชั่วโมง",                   # G
    "หมายเหตุ",                 # H
]

DONATION_SHEET = "Donations"
DONATION_HEADERS = [
    "เลขที่",            # A
    "วันเวลา",           # B
    "ผู้โดเนท",          # C
    "ID ผู้โดเนท",       # D (ซ่อน)
    "ผู้รับ",            # E
    "ID ผู้รับ",         # F (ซ่อน)
    "🎁 ยอด",           # G
    "💃 ส่วนของโฮสต์",   # H
    "🏠 เข้าร้าน",       # I
    "หมายเหตุ",         # J
    "ข้อความ",          # K
]

# ---------------------------------------------------------------- สี (ธีม OLP-Noir: ดำ-ทอง)
NOIR = "#212121"
NOIR_SOFT = "#424242"
GOLD = "#B8860B"
GOLD_LIGHT = "#FFF8E1"
GREEN, GREEN_LIGHT = "#2E7D32", "#E8F5E9"
ORANGE, ORANGE_LIGHT = "#EF6C00", "#FFF3E0"
BLUE, BLUE_LIGHT = "#1565C0", "#E3F2FD"
PINK, PINK_LIGHT = "#AD1457", "#FCE4EC"
YELLOW_LIGHT = "#FFF9C4"
GRAY_LINE = "#BDBDBD"
WHITE = "#FFFFFF"
MONEY = '#,##0.00" ฿"'


def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _range(sheet_id: int, r1: int, r2: int | None, c1: int, c2: int) -> dict:
    """ช่วงเซลล์แบบ 0-based (r2=None = ถึงแถวสุดท้าย)"""
    rng = {"sheetId": sheet_id, "startRowIndex": r1, "startColumnIndex": c1, "endColumnIndex": c2}
    if r2 is not None:
        rng["endRowIndex"] = r2
    return rng


def _cell(sheet_id, r1, r2, c1, c2, fmt: dict) -> dict:
    fields = ",".join(f"userEnteredFormat.{k}" for k in fmt)
    return {"repeatCell": {"range": _range(sheet_id, r1, r2, c1, c2), "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _header_fmt(bg: str, *, size: int = 10, fg: str = WHITE) -> dict:
    return {
        "backgroundColor": _rgb(bg),
        "textFormat": {"bold": True, "foregroundColor": _rgb(fg), "fontSize": size},
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    }


def _money(bg: str, *, bold: bool = False) -> dict:
    return {
        "backgroundColor": _rgb(bg),
        "numberFormat": {"type": "NUMBER", "pattern": MONEY},
        "textFormat": {"bold": bold},
    }


def _dimension(sheet_id: int, dim: str, index: int, props: dict) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": dim, "startIndex": index, "endIndex": index + 1},
            "properties": props,
            "fields": ",".join(props),
        }
    }


def _width(sheet_id: int, col: int, px: int) -> dict:
    return _dimension(sheet_id, "COLUMNS", col, {"pixelSize": px})


def _hide(sheet_id: int, col: int) -> dict:
    return _dimension(sheet_id, "COLUMNS", col, {"hiddenByUser": True})


def _row_height(sheet_id: int, row: int, px: int) -> dict:
    return _dimension(sheet_id, "ROWS", row, {"pixelSize": px})


def _freeze(sheet_id: int) -> dict:
    return {"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }}


def _row_rule(sheet_id: int, formula: str, bg: str, index: int, columns: int) -> dict:
    """ไฮไลต์ทั้งแถวของตารางเมื่อสูตรเป็นจริง"""
    return {
        "addConditionalFormatRule": {
            "index": index,
            "rule": {
                "ranges": [_range(sheet_id, 1, None, 0, columns)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": _rgb(bg)},
                },
            },
        }
    }


def _box(sheet_id, r1, r2, c1, c2) -> dict:
    line = {"style": "SOLID_MEDIUM", "color": _rgb(GOLD)}
    return {
        "updateBorders": {
            "range": _range(sheet_id, r1, r2, c1, c2),
            "top": line, "bottom": line, "left": line, "right": line,
            "innerHorizontal": {"style": "SOLID", "color": _rgb(GRAY_LINE)},
        }
    }


def _filter(sheet_id: int, columns: int) -> dict:
    return {"setBasicFilter": {"filter": {"range": _range(sheet_id, 0, None, 0, columns)}}}


def cycle_style_requests(sheet_id: int) -> list[dict]:
    """คำสั่งจัดรูปแบบชีตรอบบิล"""
    K, L, M, N = 10, 11, 12, 13
    Q, R, S = 16, 17, 18
    reqs: list[dict] = [
        _freeze(sheet_id),
        _row_height(sheet_id, 0, 40),
        # หัวตาราง: ดำ ตัวขาว — คอลัมน์เงินแยกสี เขียว (เข้า) / ส้ม (จ่ายพนักงาน) / น้ำเงิน (ร้าน)
        _cell(sheet_id, 0, 1, 0, 16, _header_fmt(NOIR)),
        _cell(sheet_id, 0, 1, K, K + 1, _header_fmt(GREEN)),
        _cell(sheet_id, 0, 1, L, L + 1, _header_fmt(ORANGE)),
        _cell(sheet_id, 0, 1, M, M + 1, _header_fmt(BLUE)),
        # เนื้อตาราง
        _cell(sheet_id, 1, None, 0, 16, {"verticalAlignment": "MIDDLE"}),
        _cell(sheet_id, 1, None, 0, 1, {"horizontalAlignment": "CENTER", "textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, 1, 4, {"horizontalAlignment": "CENTER"}),
        _cell(sheet_id, 1, None, 6, 7, {"textFormat": {"bold": True}}),  # ชื่อพนักงาน
        _cell(sheet_id, 1, None, 8, 9, {"wrapStrategy": "WRAP"}),        # บริการ
        _cell(sheet_id, 1, None, K, K + 1, _money(GREEN_LIGHT)),
        _cell(sheet_id, 1, None, L, L + 1, _money(ORANGE_LIGHT)),
        _cell(sheet_id, 1, None, M, M + 1, _money(BLUE_LIGHT, bold=True)),
        _cell(sheet_id, 1, None, N, N + 2, {"horizontalAlignment": "CENTER"}),
        # กล่องสรุป
        {"unmergeCells": {"range": _range(sheet_id, 0, 1, Q, S + 1)}},
        {"mergeCells": {"range": _range(sheet_id, 0, 1, Q, S + 1), "mergeType": "MERGE_ALL"}},
        _cell(sheet_id, 0, 1, Q, S + 1, _header_fmt(GOLD, size=12)),
        _cell(sheet_id, 1, 5, Q, Q + 1, {"backgroundColor": _rgb(GOLD_LIGHT), "textFormat": {"bold": True}}),
        _cell(sheet_id, 1, 5, R, R + 1, {
            "numberFormat": {"type": "NUMBER", "pattern": MONEY},
            "textFormat": {"bold": True, "fontSize": 11},
            "horizontalAlignment": "RIGHT",
        }),
        _cell(sheet_id, 1, 2, R, R + 1, {"backgroundColor": _rgb(GREEN_LIGHT), "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": _rgb(GREEN)}}),
        _cell(sheet_id, 2, 3, R, R + 1, {"backgroundColor": _rgb(ORANGE_LIGHT), "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": _rgb(ORANGE)}}),
        _cell(sheet_id, 3, 4, R, R + 1, {"backgroundColor": _rgb(BLUE_LIGHT), "textFormat": {"bold": True, "fontSize": 13, "foregroundColor": _rgb(BLUE)}}),
        _cell(sheet_id, 4, 5, R, R + 1, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0 \"บิล\""}}),
        _box(sheet_id, 0, 5, Q, S + 1),
        _cell(sheet_id, 6, 7, Q, S + 1, _header_fmt(NOIR_SOFT)),
        _cell(sheet_id, 7, None, Q, Q + 1, {"textFormat": {"bold": True}}),
        _cell(sheet_id, 7, None, R, R + 1, {"numberFormat": {"type": "NUMBER", "pattern": MONEY}}),
        _cell(sheet_id, 7, None, S, S + 1, {**_money(ORANGE_LIGHT, bold=True)}),
        # ไฮไลต์ทั้งแถว: บิลต่อเวลา = เหลือง · VIP = ทองอ่อน
        _row_rule(sheet_id, '=$N2="ต่อเวลา"', YELLOW_LIGHT, 0, 16),
        _row_rule(sheet_id, '=AND($O2<>"",$O2<>"ลูกค้าทั่วไป")', GOLD_LIGHT, 1, 16),
        # ปุ่มตัวกรอง/เรียงลำดับบนหัวตาราง
        _filter(sheet_id, 16),
    ]
    widths = {0: 70, 1: 95, 2: 65, 3: 65, 4: 150, 6: 150, 8: 230, 9: 140, K: 120, L: 140, M: 120, N: 80, 14: 110, 15: 220, Q: 200, R: 140, S: 150}
    reqs += [_width(sheet_id, col, px) for col, px in widths.items()]
    reqs += [_hide(sheet_id, col) for col in (5, 7)]  # ID ลูกค้า, ID พนักงาน
    return reqs


def attendance_style_requests(sheet_id: int) -> list[dict]:
    reqs: list[dict] = [
        _freeze(sheet_id),
        _row_height(sheet_id, 0, 36),
        _cell(sheet_id, 0, 1, 0, 8, _header_fmt(NOIR)),
        _cell(sheet_id, 0, 1, 6, 7, _header_fmt(GREEN)),
        _cell(sheet_id, 1, None, 0, 4, {"horizontalAlignment": "CENTER"}),
        _cell(sheet_id, 1, None, 4, 5, {"textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, 6, 7, {
            "numberFormat": {"type": "NUMBER", "pattern": '0.00 "ชม."'},
            "backgroundColor": _rgb(GREEN_LIGHT),
            "textFormat": {"bold": True},
            "horizontalAlignment": "CENTER",
        }),
        _filter(sheet_id, 8),
    ]
    widths = {0: 70, 1: 100, 2: 140, 3: 160, 4: 170, 6: 100, 7: 240}
    reqs += [_width(sheet_id, col, px) for col, px in widths.items()]
    reqs.append(_hide(sheet_id, 5))
    return reqs


def donation_style_requests(sheet_id: int) -> list[dict]:
    reqs: list[dict] = [
        _freeze(sheet_id),
        _row_height(sheet_id, 0, 36),
        _cell(sheet_id, 0, 1, 0, 11, _header_fmt(PINK)),
        _cell(sheet_id, 0, 1, 7, 8, _header_fmt(ORANGE)),
        _cell(sheet_id, 0, 1, 8, 9, _header_fmt(BLUE)),
        _cell(sheet_id, 1, None, 0, 2, {"horizontalAlignment": "CENTER"}),
        _cell(sheet_id, 1, None, 2, 3, {"textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, 4, 5, {"textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, 6, 7, _money(PINK_LIGHT, bold=True)),
        _cell(sheet_id, 1, None, 7, 8, _money(ORANGE_LIGHT)),
        _cell(sheet_id, 1, None, 8, 9, _money(BLUE_LIGHT)),
        _cell(sheet_id, 1, None, 10, 11, {"wrapStrategy": "WRAP"}),
        _filter(sheet_id, 11),
    ]
    widths = {0: 70, 1: 140, 2: 160, 4: 160, 6: 110, 7: 130, 8: 110, 9: 130, 10: 280}
    reqs += [_width(sheet_id, col, px) for col, px in widths.items()]
    reqs += [_hide(sheet_id, col) for col in (3, 5)]
    return reqs


class SheetsClient:
    """ห่อ gspread ให้เรียกใช้แบบ async ได้ (gspread เป็น sync ล้วน)"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = None
        self._spreadsheet = None
        self._lock = asyncio.Lock()
        self._styled: set[str] = set()  # ชีตที่จัดรูปแบบแล้วในการรันครั้งนี้

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("google_sheets.enabled", False))

    @property
    def ready(self) -> bool:
        return self._spreadsheet is not None

    # ------------------------------------------------------------- startup
    async def start(self) -> None:
        if not self.enabled:
            log.info("ปิดการใช้งาน Google Sheets (google_sheets.enabled = false)")
            return
        try:
            await asyncio.to_thread(self._connect)
            log.info("เชื่อมต่อ Google Sheets สำเร็จ")
        except Exception:  # noqa: BLE001 - ไม่ให้บอทล่มเพราะ Sheets
            log.exception("เชื่อมต่อ Google Sheets ไม่สำเร็จ — บอทจะทำงานต่อโดยไม่บันทึกชีต")

    def _connect(self) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_file = Path(self.cfg.get("google_sheets.credentials_file", "credentials.json"))
        if not creds_file.exists():
            raise FileNotFoundError(f"ไม่พบไฟล์ credential: {creds_file}")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(self.cfg.get("google_sheets.spreadsheet_id"))

    # ------------------------------------------------------------- helpers
    @staticmethod
    def cycle_name(day: dt.date) -> str:
        return f"Cycle_{day.isoformat()}"

    def _worksheet(self, title: str, *, rows: int, cols: int):
        """คืนชีต (สร้างใหม่ถ้ายังไม่มี) และบอกว่าเพิ่งสร้างหรือไม่"""
        import gspread

        assert self._spreadsheet is not None
        try:
            return self._spreadsheet.worksheet(title), False
        except gspread.WorksheetNotFound:
            return self._spreadsheet.add_worksheet(title=title, rows=rows, cols=cols), True

    def _clear_old_styles(self, sheet_id: int) -> list[dict]:
        """คำสั่งลบกฎไฮไลต์และแถบสีสลับเดิมของชีต (กันซ้ำ/ทับสีใหม่เวลาจัดรูปแบบใหม่)"""
        assert self._spreadsheet is not None
        meta = self._spreadsheet.fetch_sheet_metadata(
            {"fields": "sheets(properties.sheetId,conditionalFormats,bandedRanges.bandedRangeId)"}
        )
        sheet = next((s for s in meta.get("sheets", []) if s["properties"]["sheetId"] == sheet_id), {})
        count = len(sheet.get("conditionalFormats", []))
        reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}} for _ in range(count)]
        reqs += [{"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}} for b in sheet.get("bandedRanges", [])]
        return reqs

    def _style(self, ws, kind: str) -> None:
        """เขียนหัวตาราง + จัดรูปแบบทั้งชีต (ข้อมูลเดิมไม่หาย)"""
        assert self._spreadsheet is not None
        if kind == "cycle":
            ws.update(values=[HEADERS], range_name="A1")
            ws.update(values=SUMMARY_BLOCK, range_name="Q1", value_input_option="USER_ENTERED")
            requests = cycle_style_requests(ws.id)
        elif kind == "attendance":
            ws.update(values=[ATTENDANCE_HEADERS], range_name="A1")
            requests = attendance_style_requests(ws.id)
        else:
            ws.update(values=[DONATION_HEADERS], range_name="A1")
            requests = donation_style_requests(ws.id)
        self._spreadsheet.batch_update({"requests": self._clear_old_styles(ws.id) + requests})
        self._styled.add(ws.title)

    def _ensure_styled(self, ws, kind: str) -> None:
        """จัดรูปแบบครั้งแรกที่ใช้ชีตในการรันนี้ (ชีตเก่าที่สร้างก่อนอัปเดตก็ได้รูปแบบใหม่ด้วย)"""
        if ws.title in self._styled:
            return
        try:
            self._style(ws, kind)
        except Exception:  # noqa: BLE001 - จัดรูปแบบไม่ได้ก็ไม่เป็นไร ข้อมูลสำคัญกว่า
            log.exception("จัดรูปแบบชีต %s ไม่สำเร็จ", ws.title)
            self._styled.add(ws.title)

    def _cycle_ws(self, title: str):
        ws, _ = self._worksheet(title, rows=500, cols=26)
        self._ensure_styled(ws, "cycle")
        return ws

    def _attendance_ws(self):
        ws, _ = self._worksheet(ATTENDANCE_SHEET, rows=1000, cols=8)
        self._ensure_styled(ws, "attendance")
        return ws

    def _donation_ws(self):
        ws, _ = self._worksheet(DONATION_SHEET, rows=1000, cols=len(DONATION_HEADERS))
        self._ensure_styled(ws, "donation")
        return ws

    @staticmethod
    def _append_below(ws, row: list, last_col: str) -> None:
        """เขียนต่อจากแถวสุดท้ายของคอลัมน์ A — ไม่ใช้ append_row เพราะ Google จะนับกล่องสรุปด้านขวา
        เป็นส่วนของตารางแล้วแทรกแถวว่าง"""
        line = len(ws.col_values(1)) + 1
        if line > ws.row_count:
            ws.add_rows(200)
        ws.update(values=[row], range_name=f"A{line}:{last_col}{line}", value_input_option="USER_ENTERED")

    @staticmethod
    def _upsert(ws, row: list, last_col: str) -> None:
        """ถ้าเลขที่ (คอลัมน์ A) เคยลงแล้วให้แก้แถวเดิม ไม่เพิ่มแถวซ้ำ (เช่น แอดมินแก้เวลาเข้างาน)"""
        ids = ws.col_values(1)
        key = str(row[0])
        if key in ids[1:]:
            line = ids.index(key, 1) + 1
            ws.update(values=[row], range_name=f"A{line}:{last_col}{line}", value_input_option="USER_ENTERED")
        else:
            SheetsClient._append_below(ws, row, last_col)

    async def _run(self, what: str, fn, *args) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(fn, *args)
                return True
            except Exception:  # noqa: BLE001
                log.exception("%s ไม่สำเร็จ", what)
                return False

    # -------------------------------------------------------------- public
    async def append_job_row(self, sheet_title: str, row: list) -> bool:
        return await self._run(
            "บันทึกบิลลง Google Sheets", lambda: self._append_below(self._cycle_ws(sheet_title), row, "P")
        )

    async def append_attendance_row(self, row: list) -> bool:
        return await self._run("บันทึกเวลาเข้างานลง Google Sheets", lambda: self._upsert(self._attendance_ws(), row, "H"))

    async def append_donation_row(self, row: list) -> bool:
        return await self._run("บันทึกโดเนทลง Google Sheets", lambda: self._upsert(self._donation_ws(), row, "K"))

    async def create_cycle_sheet(self, title: str) -> bool:
        return await self._run(f"สร้างชีตรอบใหม่ ({title})", lambda: self._cycle_ws(title))

    async def restyle(self, cycle_title: str) -> list[str]:
        """จัดรูปแบบชีตรอบปัจจุบัน + Attendance + Donations ใหม่ทั้งหมด — ข้อมูลเดิมไม่หาย"""
        if not self.ready:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._restyle_sync, cycle_title)

    def _restyle_sync(self, cycle_title: str) -> list[str]:
        done = []
        for title, kind, rows, cols in (
            (cycle_title, "cycle", 500, 26),
            (ATTENDANCE_SHEET, "attendance", 1000, 8),
            (DONATION_SHEET, "donation", 1000, len(DONATION_HEADERS)),
        ):
            ws, _ = self._worksheet(title, rows=rows, cols=cols)
            self._style(ws, kind)
            done.append(title)
        return done

    async def spreadsheet_url(self) -> str | None:
        if not self.ready:
            return None
        return f"https://docs.google.com/spreadsheets/d/{self.cfg.get('google_sheets.spreadsheet_id')}"

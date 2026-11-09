"""Выгрузка журнала в Excel по графам шаблона, готовая к печати."""
import io
import math
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

THIN = Side(style="thin", color="000000")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _fmt_date(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}"


def _fmt_ru(d: str | None) -> str:
    return _fmt_date(d) if d else "…"


def build(entries: list[dict], template: dict, config: dict, date_from: str | None, date_to: str | None, chain: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Журнал"
    cols = template["columns"]
    ncol = len(cols)
    last = get_column_letter(ncol)

    def title_row(row, text, size=11, bold=False):
        ws.merge_cells(f"A{row}:{last}{row}")
        c = ws[f"A{row}"]
        c.value = text
        c.font = Font(size=size, bold=bold)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    title_row(1, config.get("organization", ""))
    title_row(2, config.get("department", ""))
    title_row(3, template["name"].upper(), size=14, bold=True)
    title_row(4, f"Период: с {_fmt_ru(date_from or (entries[0]['created_at'][:10] if entries else None))} "
                 f"по {_fmt_ru(date_to or (entries[-1]['created_at'][:10] if entries else None))}")
    ws.row_dimensions[1].height = 18
    ws.row_dimensions[3].height = 24

    header_row = 6
    for i, col in enumerate(cols, start=1):
        c = ws.cell(row=header_row, column=i, value=col["title"])
        c.font = Font(bold=True, size=10)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
        c.fill = PatternFill("solid", fgColor="EDEDED")
        ws.column_dimensions[get_column_letter(i)].width = col.get("width", 15)
    ws.row_dimensions[header_row].height = 48

    text_width = next((c.get("width", 60) for c in cols if c["key"] == "text"), 60)
    row = header_row + 1
    for e in entries:
        erroneous = bool(e.get("erroneous_by"))
        text = f"({e['text']})" if erroneous else e["text"]
        signature = e["author_fio"] + (f"\n{e['cosigner_fio']}" if e.get("cosigner_fio") else "")
        values = {
            "n": e["id"],
            "date": _fmt_date(e["created_at"]),
            "time": e["created_at"][11:16],
            "text": text,
            "signature": signature,
            "kind": e["kind"],
        }
        for i, col in enumerate(cols, start=1):
            c = ws.cell(row=row, column=i, value=values.get(col["key"], ""))
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=True,
                                    horizontal="left" if col["key"] in ("text", "signature") else "center")
            c.font = Font(size=10, strike=erroneous and col["key"] == "text", bold=e["kind"] == "shift" and col["key"] == "text")
        chars_per_line = max(10, text_width * (0.72 if e["kind"] == "shift" else 0.8))
        lines = max(1, math.ceil(len(text) / chars_per_line), signature.count("\n") + 1)
        ws.row_dimensions[row].height = 13.5 * lines + 5
        row += 1

    row += 1
    ws.cell(row=row, column=1, value=(
        f"Проверка целостности: {'пройдена' if chain.get('ok') else 'НЕ ПРОЙДЕНА — ' + chain.get('reason', '')}. "
        f"Записей в журнале: {chain.get('count', 0)}."
    )).font = Font(size=9, italic=True)
    ws.cell(row=row + 1, column=1, value=(
        f"Форма: {template['name']}, версия {template['version']} (действует с {_fmt_ru(template.get('effective_from'))}). "
        f"Выгружено {datetime.now():%d.%m.%Y %H:%M}."
    )).font = Font(size=9, italic=True)
    ws.cell(row=row + 2, column=1, value="Ошибочные записи взяты в скобки и зачёркнуты так, что текст остаётся читаемым.").font = Font(size=9, italic=True)

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    ws.print_title_rows = f"{header_row}:{header_row}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.5
    ws.oddFooter.center.text = "Лист &P из &N"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

"""База продуктов ↔ Excel: выгрузка в xlsx и загрузка поправленного файла обратно.

Хранилище одно — база бота. Таблица — способ посмотреть и поправить много строк сразу:
строки без изменений не трогаются, строки, которых нет в файле, остаются как были.
"""
from __future__ import annotations

from html import escape
from io import BytesIO

import sqlite3

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from .. import db
from .foodbase import SOURCES
from .parser import normalize

COLS = [("id", 7), ("Название", 30), ("Синонимы (через запятую)", 32), ("Ккал", 8), ("Белки", 8), ("Жиры", 8),
        ("Углеводы", 10), ("Штрихкод", 16), ("Упаковка, г", 12), ("В быстрой записи", 16), ("Источник", 15),
        ("Раз ели", 9), ("Последний раз", 14), ("Удалить", 9)]
EDITABLE = ("name", "aliases", "kcal", "protein", "fat", "carbs", "barcode", "pack", "pinned")
LABELS = {"name": "название", "aliases": "синонимы", "kcal": "ккал", "protein": "белки", "fat": "жиры",
          "carbs": "углеводы", "barcode": "штрихкод", "pack": "упаковка", "pinned": "в быстрой записи"}


def export(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Продукты"
    ws.append([c for c, _ in COLS])
    head = PatternFill("solid", fgColor="E8EEF7")
    for i, (_, w) in enumerate(COLS, 1):
        cell = ws.cell(1, i)
        cell.font, cell.fill = Font(bold=True), head
        ws.column_dimensions[cell.column_letter].width = w
    grey = Font(color="888888")
    for r in rows:
        ws.append([r["id"], r["name"], r.get("aliases") or "", r["kcal"], r["protein"], r["fat"], r["carbs"],
                   r.get("barcode") or "", r.get("pack") or None, "да" if r["pinned"] else "нет",
                   SOURCES.get(r["source"], r["source"]), r["uses"], (r.get("last_used") or "")[:10], ""])
        n = ws.max_row
        ws.cell(n, 8).number_format = "@"       # штрихкод — текстом, иначе Excel превратит в 5,9E+12
        for c in (1, 11, 12, 13):
            ws.cell(n, c).font = grey
    yes_no = DataValidation(type="list", formula1='"да,нет"', allow_blank=True)
    ws.add_data_validation(yes_no)
    yes_no.add(f"J2:J{max(2, ws.max_row + 200)}")
    yes_no.add(f"N2:N{max(2, ws.max_row + 200)}")
    for row in ws.iter_rows(min_row=ws.max_row + 1, max_row=ws.max_row + 200, min_col=8, max_col=8):
        row[0].number_format = "@"
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions

    hw = wb.create_sheet("Как пользоваться")
    for line in (
        "Цифры — на 100 г продукта.",
        "Поправить: меняйте значения в строке и пришлите файл боту в чат.",
        "Новый продукт: новая строка без id.",
        "Синонимы: как вы называете продукт («филе курицы, куриное филе») — по ним бот узнаёт его в тексте.",
        "Одно название у разных упаковок — нормально, их различает штрихкод; по тексту берётся последняя использованная.",
        "«В быстрой записи» = да — продукт появится кнопкой в «⚡ Быстрая запись».",
        "Удалить: «да» в последнем столбце. Строки, которые вы стёрли из файла, бот НЕ удаляет.",
        "Серые столбцы (id, источник, раз ели, последний раз) только для справки.",
    ):
        hw.append([line])
    hw.column_dimensions["A"].width = 110
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _num(v, field: str, lo: float, hi: float, errors: list, n: int) -> float | None:
    if v is None or str(v).strip() == "":
        errors.append(f"строка {n}: нет значения «{LABELS[field]}»")
        return None
    try:
        x = float(str(v).replace(",", ".").strip())
    except ValueError:
        errors.append(f"строка {n}: «{LABELS[field]}» = «{v}» — не число")
        return None
    if not lo <= x <= hi:
        errors.append(f"строка {n}: «{LABELS[field]}» = {x:g} — вне {lo:g}–{hi:g}")
        return None
    return round(x, 1)


def _barcode(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip().replace(" ", "")


def parse(data: bytes, current: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Файл → (изменения, ошибки, предупреждения). Изменение: {op: new|upd|del, id, name, fields}."""
    try:
        ws = load_workbook(BytesIO(data), data_only=True).worksheets[0]
    except Exception:
        return [], ["не получилось открыть файл — нужен .xlsx, выгруженный из бота"], []
    header = [str(c.value or "").strip() for c in ws[1]]
    if header[:len(COLS)] != [c for c, _ in COLS]:
        return [], ["столбцы не совпадают с выгрузкой — не переставляйте и не переименовывайте их"], []
    by_id = {r["id"]: r for r in current}
    changes, errors, warns, seen = [], [], [], {}
    for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        row = list(row) + [None] * (len(COLS) - len(row))
        pid, name, aliases, kcal, prot, fat, carbs, code, pack, pinned, *_rest, delete = row[:len(COLS)]
        if all(v in (None, "") for v in row[:10]):
            continue
        pid = int(pid) if isinstance(pid, (int, float)) and int(pid) in by_id else None
        if str(delete or "").strip().lower() == "да":
            if pid:
                changes.append({"op": "del", "id": pid, "name": by_id[pid]["name"], "fields": {}})
            continue
        name = normalize(str(name or ""))[:60]
        if not name:
            errors.append(f"строка {n}: нет названия")
            continue
        bad = len(errors)
        f = {"name": name,
             "aliases": ", ".join(a for a in (normalize(x) for x in str(aliases or "").split(",")) if a),
             "kcal": _num(kcal, "kcal", 0, 900, errors, n), "protein": _num(prot, "protein", 0, 100, errors, n),
             "fat": _num(fat, "fat", 0, 100, errors, n), "carbs": _num(carbs, "carbs", 0, 100, errors, n)}
        code = _barcode(code)
        if code and not (code.isdigit() and len(code) in (8, 12, 13, 14)):
            errors.append(f"строка {n}: штрихкод «{code}» — нужно 8, 12, 13 или 14 цифр")
        f["barcode"] = code or None
        f["pack"] = None
        if pack not in (None, ""):
            f["pack"] = _num(pack, "pack", 1, 5000, errors, n)
        pin = str(pinned or "").strip().lower()
        f["pinned"] = 1 if pin == "да" else 0 if pin == "нет" else (by_id[pid]["pinned"] if pid else 0)
        if len(errors) > bad:
            continue
        if f["protein"] + f["fat"] + f["carbs"] > 105:
            errors.append(f"строка {n}: белки + жиры + углеводы больше 100 г на 100 г")
            continue
        key = (name, f["barcode"] or "")
        if key in seen:
            errors.append(f"строка {n}: «{name}» с таким же штрихкодом уже есть в строке {seen[key]}")
            continue
        seen[key] = n
        energy = 4 * f["protein"] + 9 * f["fat"] + 4 * f["carbs"]
        if f["kcal"] > 40 and abs(energy - f["kcal"]) / f["kcal"] > 0.3:
            warns.append(f"«{name}»: по БЖУ выходит ≈{energy:.0f} ккал, а указано {f['kcal']:g} — проверьте")
        if pid:
            old = by_id[pid]
            diff = {k: v for k, v in f.items() if (old.get(k) or None) != (v or None)}
            if diff:
                changes.append({"op": "upd", "id": pid, "name": old["name"], "fields": diff})
        else:
            changes.append({"op": "new", "id": None, "name": name, "fields": f})
    # конфликты с базой: новая строка с тем же названием и штрихкодом, что у существующей
    taken = {(r["name"], r.get("barcode") or ""): r["id"] for r in current}
    for c in changes:
        if c["op"] == "new":
            other = taken.get((c["name"], c["fields"]["barcode"] or ""))
            if other:
                errors.append(f"«{c['name']}» уже есть в базе (id {other}) — правьте ту строку")
    changes = [c for c in changes if not (c["op"] == "new" and taken.get((c["name"], c["fields"]["barcode"] or "")))]
    return changes, errors, warns


def _show(v) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return {1: "да", 0: "нет"}.get(v, str(v)) if isinstance(v, int) else str(v)


def summary(changes: list[dict], current: list[dict], errors: list[str], warns: list[str]) -> str:
    by_id = {r["id"]: r for r in current}
    groups = {"upd": [], "new": [], "del": []}
    for c in changes:
        if c["op"] == "upd":
            old = by_id[c["id"]]
            parts = [f"{LABELS[k]} {_show(old.get(k))} → {_show(v)}" for k, v in c["fields"].items()]
            groups["upd"].append(escape(f"• {c['name']}: " + "; ".join(parts)))
        elif c["op"] == "new":
            f = c["fields"]
            groups["new"].append(escape(f"• {f['name']} — {f['kcal']:g} ккал, Б {f['protein']:g} Ж {f['fat']:g} "
                                        f"У {f['carbs']:g}" + (f", штрихкод {f['barcode']}" if f["barcode"] else "")))
        else:
            groups["del"].append(escape(f"• {c['name']}"))
    lines = ["📗 <b>Таблица продуктов</b>"]
    for op, title in (("upd", "Изменить"), ("new", "Добавить"), ("del", "Удалить")):
        if groups[op]:
            lines += ["", f"<b>{title}: {len(groups[op])}</b>"] + groups[op][:15]
            if len(groups[op]) > 15:
                lines.append(f"… и ещё {len(groups[op]) - 15}")
    if not changes:
        lines.append("Изменений нет.")
    if errors:
        lines += ["", f"⚠️ <b>Пропущу {len(errors)}:</b>"] + [escape(f"• {e}") for e in errors[:10]]
    if warns:
        lines += ["", "💡 <b>Проверьте:</b>"] + [escape(f"• {w}") for w in warns[:10]]
    return "\n".join(lines)


async def apply(changes: list[dict]) -> tuple[int, list[str]]:
    """Применить изменения. (сколько применено, ошибки)."""
    done, errors = 0, []
    for c in changes:
        f = dict(c["fields"])
        try:
            if c["op"] == "del":
                await db.delete_product(c["id"])
            elif c["op"] == "upd":
                if set(f) & {"kcal", "protein", "fat", "carbs"}:
                    f["source"] = "manual"   # ручная правка — главнее оценки и правок по ходу
                await db.update_product(c["id"], **f)
            else:
                await db.add_product(f["name"], f["kcal"], f["protein"], f["fat"], f["carbs"], f["barcode"], f["pack"],
                                     source="manual", pinned=f["pinned"], aliases=f["aliases"])
            done += 1
        except sqlite3.IntegrityError:
            errors.append(f"«{c['name']}»: такое название с таким штрихкодом уже есть")
    return done, errors

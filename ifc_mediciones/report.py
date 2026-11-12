"""Group measurement lines into partidas and write them to Excel / CSV."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .extract import Line, Result
from .rules import RuleSet

UNIT_LABEL = {"m3": "m³", "m2": "m²", "m": "m", "ud": "ud"}


@dataclass
class MeasureRow:
    """One line of a partida: elements of the same type on the same storey."""

    planta: str
    tipo: str
    n: int
    cantidad: float
    fuentes: str


@dataclass
class Partida:
    codigo: str
    capitulo: str
    resumen: str
    unidad: str
    rows: list[MeasureRow]

    @property
    def total(self) -> float:
        return sum(r.cantidad for r in self.rows)

    @property
    def n(self) -> int:
        return sum(r.n for r in self.rows)


def build_partidas(result: Result, rules: RuleSet) -> list[Partida]:
    by_code: dict[str, dict[tuple[str, str], list[Line]]] = defaultdict(lambda: defaultdict(list))
    for line in result.lines:
        if line.cantidad is None:
            continue
        by_code[line.codigo][(line.planta, line.tipo)].append(line)

    partidas = []
    for rule in rules.partidas:  # keep the order of the rules file
        groups = by_code.get(rule.codigo)
        if not groups:
            continue
        rows = []
        for (planta, tipo), lines in sorted(groups.items(), key=lambda kv: (_storey_key(kv[0][0]), kv[0][1])):
            fuentes = sorted({_short_source(l.fuente) for l in lines})
            rows.append(MeasureRow(planta, tipo, len(lines), sum(l.cantidad for l in lines), ", ".join(fuentes)))
        partidas.append(Partida(rule.codigo, rule.capitulo, rule.resumen, rule.unidad, rows))
    return sorted(partidas, key=lambda p: p.codigo)


def _storey_key(name: str):
    return (name == "Sin planta", name)


def _short_source(fuente: str) -> str:
    if fuente.startswith("geometría"):
        return "geometría"
    if fuente == "recuento":
        return "recuento"
    return "IFC"


# -- Excel ------------------------------------------------------------------

HEADER_FILL = PatternFill("solid", fgColor="3E3E42")
CHAPTER_FILL = PatternFill("solid", fgColor="D9D9D9")
PARTIDA_FILL = PatternFill("solid", fgColor="F2F2F2")
WHITE_BOLD = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
THIN_TOP = Border(top=Side(style="thin"))
NUM = "#,##0.00"


def write_excel(result: Result, rules: RuleSet, path: str | Path) -> None:
    partidas = build_partidas(result, rules)
    wb = Workbook()

    _sheet_mediciones(wb.active, partidas, rules, result)
    _sheet_resumen(wb.create_sheet("Resumen"), partidas, rules)
    _sheet_detalle(wb.create_sheet("Detalle"), result)
    _sheet_incidencias(wb.create_sheet("Incidencias"), result)
    wb.save(path)


def _header(ws, labels, widths):
    ws.append(labels)
    for i, w in enumerate(widths, start=1):
        cell = ws.cell(row=ws.max_row, column=i)
        cell.fill, cell.font = HEADER_FILL, WHITE_BOLD
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = f"A{ws.max_row + 1}"


def _sheet_mediciones(ws, partidas, rules, result):
    ws.title = "Mediciones"
    ws.append([f"Mediciones — {result.model}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Generado con ifc-mediciones a partir del modelo IFC. Revisar la hoja Incidencias antes de usar."])
    ws["A2"].font = Font(italic=True, color="666666")
    ws.append([])
    _header(ws, ["Código", "Ud", "Resumen / Planta", "Tipo", "N", "Cantidad", "Fuente"], [10, 6, 46, 46, 6, 13, 12])

    current = None
    for p in partidas:
        if p.capitulo != current:
            current = p.capitulo
            ws.append([p.capitulo, "", rules.capitulos.get(p.capitulo, "").upper()])
            for c in ws[ws.max_row]:
                c.fill, c.font = CHAPTER_FILL, BOLD

        ws.append([p.codigo, UNIT_LABEL[p.unidad], p.resumen])
        for c in ws[ws.max_row]:
            c.fill, c.font = PARTIDA_FILL, BOLD

        for r in p.rows:
            ws.append(["", "", r.planta, r.tipo, r.n, round(r.cantidad, 3), r.fuentes])
            ws.cell(row=ws.max_row, column=6).number_format = NUM

        ws.append(["", "", f"Total {p.codigo}", "", p.n, round(p.total, 3)])
        for c in ws[ws.max_row]:
            c.font, c.border = BOLD, THIN_TOP
        ws.cell(row=ws.max_row, column=6).number_format = NUM
        ws.append([])

    for row in ws.iter_rows(min_row=5):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=c.column in (3, 4))


def _sheet_resumen(ws, partidas, rules):
    _header(ws, ["Capítulo", "Código", "Resumen", "Ud", "N elementos", "Cantidad"], [26, 10, 50, 6, 12, 13])
    for p in partidas:
        ws.append([rules.capitulos.get(p.capitulo, p.capitulo), p.codigo, p.resumen, UNIT_LABEL[p.unidad], p.n, round(p.total, 3)])
        ws.cell(row=ws.max_row, column=6).number_format = NUM


def _sheet_detalle(ws, result):
    _header(ws, ["GlobalId", "Clase IFC", "Nombre", "Tipo", "Planta", "Partida", "Ud", "Cantidad", "Fuente"],
            [24, 22, 36, 36, 16, 9, 6, 12, 34])
    for l in result.lines:
        ws.append([l.global_id, l.clase, l.nombre, l.tipo, l.planta, l.codigo, UNIT_LABEL[l.unidad],
                   None if l.cantidad is None else round(l.cantidad, 4), l.fuente])
        ws.cell(row=ws.max_row, column=8).number_format = "#,##0.000"
    ws.auto_filter.ref = ws.dimensions


def _sheet_incidencias(ws, result):
    _header(ws, ["Incidencia", "GlobalId", "Clase IFC", "Detalle"], [22, 24, 24, 90])
    for i in result.incidencias:
        ws.append([i.codigo, i.global_id, i.clase, i.detalle])
    if result.included_in_parent:
        ws.append(["INFO", "", "", f"{result.included_in_parent} elemento(s) no se miden por separado porque forman parte de otro elemento ya medido (p. ej. tramos de escalera, paneles de muro cortina)."])
    ws.auto_filter.ref = ws.dimensions


# -- CSV --------------------------------------------------------------------

def write_csv(result: Result, rules: RuleSet, path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["codigo", "capitulo", "resumen", "ud", "n", "cantidad"])
        for p in build_partidas(result, rules):
            w.writerow([p.codigo, rules.capitulos.get(p.capitulo, ""), p.resumen, p.unidad, p.n, f"{p.total:.3f}".replace(".", ",")])

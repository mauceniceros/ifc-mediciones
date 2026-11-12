"""Command line: ifc-mediciones model.ifc -o mediciones.xlsx"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .extract import extract
from .report import build_partidas, write_csv, write_excel
from .rules import load_rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ifc-mediciones",
        description="Extract a Spanish-style bill of quantities (mediciones) from an IFC model.",
    )
    parser.add_argument("ifc", type=Path, help="IFC file (IFC2x3, IFC4 or IFC4.3)")
    parser.add_argument("-o", "--output", type=Path, help="Excel output (default: <model>_mediciones.xlsx)")
    parser.add_argument("--csv", type=Path, help="also write the summary as CSV")
    parser.add_argument("--reglas", type=Path, help="custom rules YAML (default: built-in partidas.yaml)")
    parser.add_argument("--verificar", action="store_true",
                        help="cross-check IFC quantities against the geometry (slower)")
    parser.add_argument("--tolerancia", type=float, default=0.10,
                        help="relative difference that counts as a discrepancy (default 0.10)")
    args = parser.parse_args(argv)

    if not args.ifc.exists():
        parser.error(f"file not found: {args.ifc}")

    rules = load_rules(args.reglas)
    result = extract(args.ifc, rules, verify=args.verificar, tolerance=args.tolerancia)

    output = args.output or args.ifc.with_name(f"{args.ifc.stem}_mediciones.xlsx")
    write_excel(result, rules, output)
    if args.csv:
        write_csv(result, rules, args.csv)

    partidas = build_partidas(result, rules)
    print(f"{result.model} ({result.schema}): {len(result.lines)} elementos medidos en {len(partidas)} partidas")
    for p in partidas:
        print(f"  {p.codigo:<7} {p.resumen[:48]:<48} {p.total:>12,.2f} {p.unidad}")
    kinds = Counter(i.codigo for i in result.incidencias)
    if kinds:
        print("Incidencias: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print(f"-> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Load the partida rules and decide which rule an IFC element falls under."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import ifcopenshell.util.element as uel
import yaml

UNITS = {"m3", "m2", "m", "ud"}
GEOMETRY_METHODS = {"volume", "footprint_area", "side_area", "length", "none"}


@dataclass
class Rule:
    codigo: str
    capitulo: str
    resumen: str
    unidad: str
    clases: list[str]
    cantidades: list[str] = field(default_factory=list)
    geometria: str = "none"
    tipo_predefinido: str | None = None
    exterior: bool | None = None
    tipo_regex: str | None = None

    def matches(self, element) -> bool:
        if not any(element.is_a(c) for c in self.clases):
            return False
        if self.tipo_predefinido is not None:
            if uel.get_predefined_type(element) != self.tipo_predefinido:
                return False
        if self.exterior is not None:
            if is_external(element) is not self.exterior:
                return False
        if self.tipo_regex is not None:
            if not re.search(self.tipo_regex, type_name(element), re.IGNORECASE):
                return False
        return True


@dataclass
class RuleSet:
    capitulos: dict[str, str]
    partidas: list[Rule]
    medir_siempre: list[str] = field(default_factory=list)

    def match(self, element) -> Rule | None:
        for rule in self.partidas:
            if rule.matches(element):
                return rule
        return None

    @property
    def classes(self) -> set[str]:
        return {c for r in self.partidas for c in r.clases}


def type_name(element) -> str:
    """Name of the element's type, falling back to ObjectType or Name."""
    element_type = uel.get_type(element)
    if element_type is not None and element_type.Name:
        return element_type.Name
    return getattr(element, "ObjectType", None) or element.Name or element.is_a()


def is_external(element) -> bool | None:
    """Read IsExternal from the element's Pset_*Common (instance, then type)."""
    psets = uel.get_psets(element)
    for name, values in psets.items():
        if name.startswith("Pset_") and name.endswith("Common") and "IsExternal" in values:
            return bool(values["IsExternal"])
    return None


def load_rules(path: str | Path | None = None) -> RuleSet:
    if path is None:
        text = resources.files("ifc_mediciones").joinpath("config/partidas.yaml").read_text("utf-8")
    else:
        text = Path(path).read_text("utf-8")
    data = yaml.safe_load(text)

    capitulos = {str(k): v for k, v in data.get("capitulos", {}).items()}
    partidas, seen = [], set()
    for raw in data["partidas"]:
        rule = Rule(
            codigo=str(raw["codigo"]),
            capitulo=str(raw["capitulo"]),
            resumen=raw["resumen"],
            unidad=raw["unidad"],
            clases=list(raw["clases"]),
            cantidades=list(raw.get("cantidades", [])),
            geometria=raw.get("geometria", "none"),
            tipo_predefinido=raw.get("tipo_predefinido"),
            exterior=raw.get("exterior"),
            tipo_regex=raw.get("tipo_regex"),
        )
        _validate(rule, capitulos, seen)
        seen.add(rule.codigo)
        partidas.append(rule)
    return RuleSet(capitulos, partidas, list(data.get("medir_siempre", [])))


def _validate(rule: Rule, capitulos: dict[str, str], seen: set[str]) -> None:
    where = f"partida {rule.codigo}"
    if rule.codigo in seen:
        raise ValueError(f"{where}: duplicated code")
    if rule.unidad not in UNITS:
        raise ValueError(f"{where}: unit '{rule.unidad}' must be one of {sorted(UNITS)}")
    if rule.geometria not in GEOMETRY_METHODS:
        raise ValueError(f"{where}: geometria '{rule.geometria}' must be one of {sorted(GEOMETRY_METHODS)}")
    if rule.capitulo not in capitulos:
        raise ValueError(f"{where}: chapter '{rule.capitulo}' is not listed under capitulos")
    if rule.tipo_regex is not None:
        try:
            re.compile(rule.tipo_regex)
        except re.error as exc:
            raise ValueError(f"{where}: invalid tipo_regex: {exc}") from exc
    if rule.unidad != "ud" and not rule.cantidades and rule.geometria == "none":
        raise ValueError(f"{where}: needs 'cantidades' or a 'geometria' fallback")

"""Read an IFC model and produce one measurement line per element.

Every quantity carries its source (which IFC quantity it came from, or that
it was computed from geometry), and anything doubtful becomes an
"incidencia" for a person to review instead of being silently guessed.
"""

from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as uel
import ifcopenshell.util.shape as ushape
import ifcopenshell.util.unit as uunit

from .rules import Rule, RuleSet, is_external, type_name

# Classes that are never measured on their own (voids, annotations, etc.).
IGNORED = ("IfcFeatureElement", "IfcVirtualElement")

# IFC quantity class -> project unit type used to convert it to SI.
QUANTITY_UNIT = {
    "IfcQuantityLength": "LENGTHUNIT",
    "IfcQuantityArea": "AREAUNIT",
    "IfcQuantityVolume": "VOLUMEUNIT",
}

FUZZY_CUTOFF = 0.85

# When an element has no shape of its own (a roof or curtain wall defined by
# its parts), areas are summed over these surface-forming parts only.
SURFACE_PARTS = ("IfcSlab", "IfcPlate", "IfcCovering")


@dataclass
class Line:
    global_id: str
    clase: str
    nombre: str
    tipo: str
    planta: str
    codigo: str
    unidad: str
    cantidad: float | None
    fuente: str


@dataclass
class Incidencia:
    codigo: str  # short machine-readable kind, e.g. SIN_CANTIDAD
    global_id: str
    clase: str
    detalle: str


@dataclass
class Result:
    model: str
    schema: str
    lines: list[Line] = field(default_factory=list)
    incidencias: list[Incidencia] = field(default_factory=list)
    included_in_parent: int = 0
    unmapped: Counter = field(default_factory=Counter)

    def flag(self, kind: str, element, detail: str) -> None:
        self.incidencias.append(Incidencia(kind, element.GlobalId, element.is_a(), detail))


class Extractor:
    def __init__(self, model: ifcopenshell.file, rules: RuleSet, verify: bool = False, tolerance: float = 0.10):
        self.model = model
        self.rules = rules
        self.verify = verify
        self.tolerance = tolerance
        self.scales = {t: uunit.calculate_unit_scale(model, t) for t in QUANTITY_UNIT.values()}
        # Local coordinates: walls and plates keep their thin axis aligned
        # with a local axis, which makes side areas and lengths reliable.
        self.geom_settings = ifcopenshell.geom.settings()

    # -- public -----------------------------------------------------------

    def run(self, name: str = "") -> Result:
        result = Result(model=name, schema=self.model.schema)
        seen_ids: set[str] = set()

        for element in self.model.by_type("IfcElement"):
            if any(element.is_a(c) for c in IGNORED):
                continue

            rule = self.rules.match(element)
            if rule is None:
                if not self._parent_rule(element):
                    result.unmapped[element.is_a()] += 1
                continue

            # A stair flight inside a stair, a panel inside a curtain wall...:
            # if the parent is already measured, don't count the part again.
            if self._parent_rule(element) and not any(element.is_a(c) for c in self.rules.medir_siempre):
                result.included_in_parent += 1
                continue

            if element.GlobalId in seen_ids:
                result.flag("GLOBALID_DUPLICADO", element, "El mismo GlobalId aparece más de una vez; se mide solo la primera.")
                continue
            seen_ids.add(element.GlobalId)

            result.lines.append(self._measure(element, rule, result))

        self._check_external_flags(result)

        for cls, n in sorted(result.unmapped.items()):
            result.incidencias.append(
                Incidencia("SIN_PARTIDA", "", cls, f"{n} elemento(s) de esta clase no tienen partida asignada.")
            )
        return result

    # -- measurement ------------------------------------------------------

    def _measure(self, element, rule: Rule, result: Result) -> Line:
        planta = self._storey(element)
        if planta is None:
            result.flag("SIN_PLANTA", element, "El elemento no está asociado a ninguna planta.")
            planta = "Sin planta"

        line = Line(
            global_id=element.GlobalId,
            clase=element.is_a(),
            nombre=element.Name or "",
            tipo=self._type_name(element),
            planta=planta,
            codigo=rule.codigo,
            unidad=rule.unidad,
            cantidad=None,
            fuente="",
        )

        if is_external(element) is None and any(
            r.exterior is not None and any(element.is_a(c) for c in r.clases) for r in self.rules.partidas
        ):
            result.flag("EXTERIOR_DESCONOCIDO", element, "Sin dato IsExternal: no se puede saber si es fachada o interior; asignado a la partida general.")

        if rule.unidad == "ud":
            line.cantidad, line.fuente = 1.0, "recuento"
            return line

        value, source = self._from_quantities(element, rule, result)
        if value is not None:
            line.cantidad, line.fuente = value, source
            if self.verify and rule.geometria != "none":
                self._cross_check(element, rule, value, result)
        else:
            geo, from_parts = self._geometry_or_parts(element, rule.geometria)
            if geo is not None:
                line.cantidad = geo
                line.fuente = f"geometría ({rule.geometria}{', suma de componentes' if from_parts else ''})"
                extra = " sumando sus componentes" if from_parts else ""
                result.flag("DESDE_GEOMETRIA", element, f"Sin cantidades IFC; calculado desde la geometría ({rule.geometria}){extra}. Revisar.")
            else:
                line.fuente = "sin dato"
                result.flag("SIN_CANTIDAD", element, f"No hay cantidad IFC ({', '.join(rule.cantidades)}) ni geometría utilizable.")

        if line.cantidad is not None and line.cantidad <= 0:
            hint = " La geometría del elemento parece defectuosa." if line.fuente.startswith("geometría") else ""
            result.flag("CANTIDAD_CERO", element, f"Cantidad {line.cantidad:.3f} {rule.unidad}.{hint}")
        return line

    def _from_quantities(self, element, rule: Rule, result: Result) -> tuple[float | None, str]:
        available = read_quantities(element, self.scales)
        if not available:
            return None, ""
        lower = {k.lower(): k for k in available}

        # Exact names first (in order of preference), then near-misses such as
        # "GorssVolume" exported by some tools. Near-misses are always flagged.
        choice = None
        for i, wanted in enumerate(rule.cantidades):
            if wanted.lower() in lower:
                choice = (i, wanted, lower[wanted.lower()], False)
                break
        if choice is None:
            for i, wanted in enumerate(rule.cantidades):
                close = difflib.get_close_matches(wanted.lower(), lower.keys(), n=1, cutoff=FUZZY_CUTOFF)
                if close and close[0] not in {w.lower() for w in rule.cantidades}:
                    choice = (i, wanted, lower[close[0]], True)
                    break
        if choice is None:
            return None, ""

        i, wanted, key, fuzzy = choice
        value, qset = available[key]
        if fuzzy:
            result.flag("NOMBRE_NO_ESTANDAR", element, f"Cantidad '{key}' interpretada como '{wanted}'.")
        if i > 0 and rule.cantidades[0].startswith("Net") and not wanted.startswith("Net"):
            result.flag("MEDICION_BRUTA", element, f"Sin '{rule.cantidades[0]}'; se usa '{key}' (sin descontar huecos).")
        return value, f"{qset}.{key}"
        return None, ""

    def _geometry_or_parts(self, element, method: str) -> tuple[float | None, bool]:
        """Geometry of the element, or the sum of its parts when the element
        has no shape of its own (e.g. a curtain wall made of panels)."""
        if element.Representation:
            return self._from_geometry(element, method), False
        parts = uel.get_parts(element) or []
        if method in ("side_area", "footprint_area"):
            parts = [p for p in parts if any(p.is_a(c) for c in SURFACE_PARTS)]
        values = [v for v in (self._from_geometry(p, method) for p in parts) if v is not None]
        return (sum(values), True) if values else (None, False)

    def _from_geometry(self, element, method: str) -> float | None:
        if method == "none" or not element.Representation:
            return None
        try:
            geometry = ifcopenshell.geom.create_shape(self.geom_settings, element).geometry
        except Exception:
            return None
        if method == "volume":
            return float(ushape.get_volume(geometry))
        if method == "footprint_area":
            return float(ushape.get_footprint_area(geometry))
        extents = {"X": ushape.get_x(geometry), "Y": ushape.get_y(geometry), "Z": ushape.get_z(geometry)}
        if method == "side_area":
            # Area projected along the thinnest axis: one face of a wall or
            # panel, with through-openings already subtracted.
            thin_axis = min(extents, key=extents.get)
            return float(ushape.get_footprint_area(geometry, axis=thin_axis))
        if method == "length":
            return float(max(extents.values()))
        return None

    def _cross_check(self, element, rule: Rule, value: float, result: Result) -> None:
        geo, _ = self._geometry_or_parts(element, rule.geometria)
        if not geo or not value:
            return
        diff = abs(geo - value) / max(abs(value), 1e-9)
        if diff > self.tolerance:
            result.flag(
                "DISCREPANCIA",
                element,
                f"Cantidad IFC {value:.3f} vs geometría {geo:.3f} {rule.unidad} ({diff:.0%}).",
            )

    def _check_external_flags(self, result: Result) -> None:
        """If a rule splits by IsExternal but the model marks every element the
        same way, the split is meaningless: say so instead of trusting it."""
        classes = {c for r in self.rules.partidas if r.exterior is not None for c in r.clases}
        for cls in sorted(classes):
            elements = self.model.by_type(cls)
            values = {is_external(e) for e in elements}
            if len(elements) >= 10 and len(values) == 1:
                value = {True: "exteriores", False: "interiores", None: "sin dato IsExternal"}[values.pop()]
                result.incidencias.append(Incidencia(
                    "MODELO_SOSPECHOSO", "", cls,
                    f"Los {len(elements)} elementos están marcados como {value}. La separación fachada / "
                    "interior probablemente no es fiable: revisar el parámetro IsExternal en el modelo.",
                ))

    # -- helpers ----------------------------------------------------------

    def _parent_rule(self, element) -> Rule | None:
        parent = uel.get_aggregate(element)
        while parent is not None and parent.is_a("IfcElement"):
            rule = self.rules.match(parent)
            if rule is not None:
                return rule
            parent = uel.get_aggregate(parent)
        return None

    @staticmethod
    def _storey(element) -> str | None:
        node = element
        while node is not None:
            container = uel.get_container(node)
            if container is not None:
                return container.Name or container.LongName or container.is_a()
            node = uel.get_aggregate(node)
        return None

    @staticmethod
    def _type_name(element) -> str:
        return type_name(element)


def read_quantities(element, scales: dict[str, float]) -> dict[str, tuple[float, str]]:
    """Return {quantity name: (value in SI units, quantity set name)}.

    Type-level quantities are read first so instance quantities override them.
    """
    found: dict[str, tuple[float, str]] = {}

    def collect(qset) -> None:
        if not qset.is_a("IfcElementQuantity"):
            return
        for q in qset.Quantities or []:
            if not q.is_a("IfcPhysicalSimpleQuantity"):
                continue
            raw = q[3]
            if raw is None:
                continue
            scale = 1.0
            if q.Unit is not None:
                scale = uunit.get_unit_scale(q.Unit) if hasattr(uunit, "get_unit_scale") else 1.0
            elif q.is_a() in QUANTITY_UNIT:
                scale = scales[QUANTITY_UNIT[q.is_a()]]
            found[q.Name] = (float(raw) * scale, qset.Name or "Qto")

    element_type = uel.get_type(element)
    if element_type is not None:
        for definition in getattr(element_type, "HasPropertySets", None) or []:
            collect(definition)
    for rel in getattr(element, "IsDefinedBy", None) or []:
        if rel.is_a("IfcRelDefinesByProperties"):
            definition = rel.RelatingPropertyDefinition
            for item in definition if isinstance(definition, tuple) else (definition,):
                collect(item)
    return found


def extract(path: str | Path, rules: RuleSet, verify: bool = False, tolerance: float = 0.10) -> Result:
    model = ifcopenshell.open(str(path))
    return Extractor(model, rules, verify=verify, tolerance=tolerance).run(Path(path).name)

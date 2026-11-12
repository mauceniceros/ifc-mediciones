from pathlib import Path

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.api.aggregate
import ifcopenshell.api.project
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.type
import ifcopenshell.api.unit
import ifcopenshell.util.element as uel
import ifcopenshell.util.unit as uunit
import pytest
from openpyxl import load_workbook

from ifc_mediciones import build_partidas, extract, load_rules, write_csv, write_excel
from ifc_mediciones.cli import main
from ifc_mediciones.extract import Extractor

DATA = Path(__file__).parent / "data"
ARCH_IFC4 = DATA / "Building-Architecture.ifc"
ARCH_IFC2X3 = DATA / "Building-Architecture-ifc2x3.ifc"
STRUCT_IFC4 = DATA / "Building-Structural.ifc"


@pytest.fixture(scope="module")
def rules():
    return load_rules()


def totals(result, rules):
    return {p.codigo: p.total for p in build_partidas(result, rules)}


# -- rules ------------------------------------------------------------------

def test_default_rules_load(rules):
    codes = [r.codigo for r in rules.partidas]
    assert len(codes) == len(set(codes))
    assert "IfcDoor" in rules.medir_siempre


@pytest.mark.parametrize(
    "bad, message",
    [
        ('{codigo: "1", capitulo: "01", resumen: x, unidad: kg, clases: [IfcWall]}', "unit"),
        ('{codigo: "1", capitulo: "99", resumen: x, unidad: ud, clases: [IfcWall]}', "chapter"),
        ('{codigo: "1", capitulo: "01", resumen: x, unidad: m2, clases: [IfcWall]}', "cantidades"),
    ],
)
def test_invalid_rules_are_rejected(tmp_path, bad, message):
    path = tmp_path / "rules.yaml"
    path.write_text(f'capitulos: {{"01": Test}}\npartidas:\n  - {bad}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_rules(path)


# -- quantities from real sample files ----------------------------------------

def test_slab_area_matches_ifc_quantities(rules):
    """The slab total must equal the slabs' own NetArea quantities, read independently."""
    result = extract(ARCH_IFC4, rules)
    model = ifcopenshell.open(str(ARCH_IFC4))
    area_scale = uunit.calculate_unit_scale(model, "AREAUNIT")
    measured = {l.global_id for l in result.lines if l.codigo == "02.03"}
    expected = 0.0
    for slab in model.by_type("IfcSlab"):
        if slab.GlobalId in measured:
            qtos = uel.get_psets(slab, qtos_only=True)
            expected += next(q["NetArea"] for q in qtos.values() if "NetArea" in q) * area_scale
    assert totals(result, rules)["02.03"] == pytest.approx(expected)


def test_same_building_in_ifc2x3_and_ifc4_gives_same_totals(rules):
    a = totals(extract(ARCH_IFC4, rules), rules)
    b = totals(extract(ARCH_IFC2X3, rules), rules)
    assert a.keys() == b.keys()
    for code in a:
        assert a[code] == pytest.approx(b[code], rel=1e-6)


def test_every_line_says_where_its_number_came_from(rules):
    result = extract(STRUCT_IFC4, rules)
    for line in result.lines:
        assert line.fuente, line
        if line.cantidad is None:
            assert any(i.global_id == line.global_id and i.codigo == "SIN_CANTIDAD" for i in result.incidencias)


def test_geometry_fallback_agrees_with_quantities(rules):
    """For walls that have both, the geometry side area is within 5% of the IFC quantity."""
    model = ifcopenshell.open(str(ARCH_IFC4))
    extractor = Extractor(model, rules)
    area_scale = uunit.calculate_unit_scale(model, "AREAUNIT")
    checked = 0
    for wall in model.by_type("IfcWall"):
        q = {k: v for qto in uel.get_psets(wall, qtos_only=True).values() for k, v in qto.items()}
        if "NetSideArea" not in q:
            continue
        geo = extractor._from_geometry(wall, "side_area")
        assert geo == pytest.approx(q["NetSideArea"] * area_scale, rel=0.05)
        checked += 1
    assert checked > 0


def test_parts_of_a_measured_element_are_not_counted_twice(rules):
    result = extract(ARCH_IFC4, rules)
    model = ifcopenshell.open(str(ARCH_IFC4))
    measured = {l.global_id for l in result.lines}
    for element in model.by_type("IfcElement"):
        parent = uel.get_aggregate(element)
        if parent is not None and parent.GlobalId in measured and not element.is_a("IfcDoor"):
            assert element.GlobalId not in measured


# -- behaviour on a small synthetic model ------------------------------------

def tiny_model(qto_name="NetVolume", external=None, wall_type="Muro ladrillo"):
    """A one-storey model (lengths in millimetres) with one footing and one wall."""
    f = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(f, ifc_class="IfcProject", name="Test")
    length = ifcopenshell.api.unit.add_si_unit(f, unit_type="LENGTHUNIT", prefix="MILLI")
    area = ifcopenshell.api.unit.add_si_unit(f, unit_type="AREAUNIT")
    volume = ifcopenshell.api.unit.add_si_unit(f, unit_type="VOLUMEUNIT")
    ifcopenshell.api.unit.assign_unit(f, units=[length, area, volume])
    site = ifcopenshell.api.root.create_entity(f, ifc_class="IfcSite")
    building = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuilding")
    storey = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuildingStorey", name="Planta baja")
    ifcopenshell.api.aggregate.assign_object(f, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(f, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(f, products=[storey], relating_object=building)

    footing = ifcopenshell.api.root.create_entity(f, ifc_class="IfcFooting", name="Z1")
    ifcopenshell.api.spatial.assign_container(f, products=[footing], relating_structure=storey)
    qto = ifcopenshell.api.pset.add_qto(f, product=footing, name="Qto_FootingBaseQuantities")
    ifcopenshell.api.pset.edit_qto(f, qto=qto, properties={qto_name: 1.5})

    wall_type_entity = ifcopenshell.api.root.create_entity(f, ifc_class="IfcWallType", name=wall_type)
    wall = ifcopenshell.api.root.create_entity(f, ifc_class="IfcWall", name="M1")
    ifcopenshell.api.type.assign_type(f, related_objects=[wall], relating_type=wall_type_entity)
    ifcopenshell.api.spatial.assign_container(f, products=[wall], relating_structure=storey)
    qto = ifcopenshell.api.pset.add_qto(f, product=wall, name="Qto_WallBaseQuantities")
    ifcopenshell.api.pset.edit_qto(f, qto=qto, properties={"NetSideArea": 12.0})
    if external is not None:
        pset = ifcopenshell.api.pset.add_pset(f, product=wall, name="Pset_WallCommon")
        ifcopenshell.api.pset.edit_pset(f, pset=pset, properties={"IsExternal": external})
    return f


def run(model, rules):
    return Extractor(model, rules).run("tiny")


def test_misspelled_quantity_is_used_but_flagged(rules):
    result = run(tiny_model(qto_name="GorssVolume"), rules)
    footing = next(l for l in result.lines if l.clase == "IfcFooting")
    assert footing.cantidad == pytest.approx(1.5)
    assert any(i.codigo == "NOMBRE_NO_ESTANDAR" for i in result.incidencias)


def test_gross_quantity_is_flagged(rules):
    result = run(tiny_model(qto_name="GrossVolume"), rules)
    assert any(i.codigo == "MEDICION_BRUTA" for i in result.incidencias)


@pytest.mark.parametrize("external, code", [(True, "03.01"), (False, "03.02")])
def test_is_external_splits_facade_and_partitions(rules, external, code):
    result = run(tiny_model(external=external), rules)
    wall = next(l for l in result.lines if l.clase == "IfcWall")
    assert wall.codigo == code
    assert wall.cantidad == pytest.approx(12.0)
    assert wall.planta == "Planta baja"


def test_missing_is_external_is_flagged(rules):
    result = run(tiny_model(external=None), rules)
    assert any(i.codigo == "EXTERIOR_DESCONOCIDO" for i in result.incidencias)


def test_type_regex_rule(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        'capitulos: {"03": Particiones}\n'
        "partidas:\n"
        '  - {codigo: "03.10", capitulo: "03", resumen: Pladur, unidad: m2, clases: [IfcWall],'
        " tipo_regex: pladur, cantidades: [NetSideArea]}\n"
        '  - {codigo: "03.20", capitulo: "03", resumen: Otros, unidad: m2, clases: [IfcWall],'
        " cantidades: [NetSideArea]}\n",
        encoding="utf-8",
    )
    custom = load_rules(path)
    assert run(tiny_model(wall_type="Tabique PLADUR 70"), custom).lines[0].codigo == "03.10"
    assert run(tiny_model(wall_type="Ladrillo"), custom).lines[0].codigo == "03.20"


# -- outputs ------------------------------------------------------------------

def test_excel_and_csv_outputs(tmp_path, rules):
    result = extract(ARCH_IFC4, rules)
    xlsx, csv_path = tmp_path / "out.xlsx", tmp_path / "out.csv"
    write_excel(result, rules, xlsx)
    write_csv(result, rules, csv_path)

    wb = load_workbook(xlsx)
    assert wb.sheetnames == ["Mediciones", "Resumen", "Detalle", "Incidencias"]
    resumen = {row[1]: row[5] for row in wb["Resumen"].iter_rows(min_row=2, values_only=True)}
    for code, total in totals(result, rules).items():
        assert resumen[code] == pytest.approx(total, abs=1e-3)
    assert csv_path.read_text(encoding="utf-8-sig").startswith("codigo;")


def test_cli(tmp_path, capsys):
    out = tmp_path / "m.xlsx"
    assert main([str(ARCH_IFC4), "-o", str(out), "--verificar"]) == 0
    assert out.exists()
    assert "partidas" in capsys.readouterr().out

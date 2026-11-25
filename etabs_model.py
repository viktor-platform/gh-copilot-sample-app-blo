import logging
from dataclasses import dataclass
from math import isclose, isfinite
from pathlib import Path
import sys
from typing import Any, TypedDict
from uuid import uuid4

from shed import COMBINATIONS, Model, Point, SECTIONS, Shed, WIND_COMBO, build_model, load_totals


JOINT_GROUP = "SHED-JOINTS"
STEEL = "SHED-STEEL"
PATTERNS = (("SHED-DEAD", 1, 1.0), ("SHED-ROOF-LIVE", 11, 0.0), ("SHED-WIND", 6, 0.0))
SECTION_IDS = {
    "column": "SHED-UC254-73",
    "chord": "SHED-UB305-40",
    "purlin": "SHED-UB305-40",
    "web": "SHED-CHS88.9-5",
    "roof_brace": "SHED-CHS114.3-5",
    "side_brace": "SHED-CHS114.3-5",
}


class EtabsError(RuntimeError):
    pass


class ComboResult(TypedDict):
    translations_m: list[Point]
    max_displacement_mm: float
    base_reaction_kn: Point


def check(raw: Any, operation: str) -> None:
    status = raw[-1] if isinstance(raw, (tuple, list)) and raw else raw
    if not (status is None or type(status) is int and status == 0):
        raise EtabsError(f"ETABS could not {operation} (status {status}).")


def outputs(raw: Any, count: int, operation: str) -> list[Any]:
    if not isinstance(raw, (tuple, list)) or len(raw) != count + 1:
        raise EtabsError(f"ETABS returned an unexpected output for {operation}.")
    check(raw, operation)
    return list(raw[:-1])


@dataclass(frozen=True)
class TableEdit:
    key: str
    version: int
    columns: tuple[str, ...]
    rows: list[tuple[str, ...]]


def editable_table(database: Any, catalog: dict[str, int], key: str, columns: tuple[str, ...]) -> int:
    if catalog.get(key) not in (2, 3):
        raise EtabsError(f"ETABS does not allow importing the {key} table.")
    version, count, fields, _, _, _, writable = outputs(
        database.GetAllFieldsInTable(key, 0, 0, [], [], [], [], []), 7, f"read the {key} schema",
    )
    if count != len(fields) or count != len(writable):
        raise EtabsError(f"ETABS returned an incomplete schema for {key}.")
    allowed = {field for field, can_import in zip(fields, writable, strict=True) if can_import}
    if not set(columns) <= allowed:
        raise EtabsError(f"The {key} table does not accept the columns this app imports.")
    table_version, _, existing_rows, _ = outputs(
        database.GetTableForEditingArray(key, "", 0, [], 0, []), 4, f"read the {key} edit table",
    )
    if existing_rows or table_version not in (0, version):
        raise EtabsError(f"The {key} table is not empty or changed version.")
    return version


def import_tables(database: Any, tables: list[TableEdit]) -> None:
    check(database.CancelTableEditing(), "clear staged database edits")
    try:
        for table in tables:
            flat = [cell for row in table.rows for cell in row]
            check(database.SetTableForEditingArray(
                table.key, table.version, list(table.columns), len(table.rows), flat,
            ), f"stage {table.key}")
    except Exception:
        database.CancelTableEditing()
        raise
    fatal, errors, warnings, _, log = outputs(
        database.ApplyEditedTables(True, 0, 0, 0, 0, ""), 5, "apply the database edits",
    )
    if fatal or errors:
        raise EtabsError(f"ETABS import reported {fatal} fatal errors and {errors} errors. {str(log)[-800:]}")
    if warnings:
        logging.getLogger(__name__).warning("ETABS database import: %s", log)


def create_points(sap: Any, model: Model, database: Any, catalog: dict[str, int]) -> list[str]:
    key = "Point Object Connectivity"
    columns = ("UniqueName", "X", "Y", "Z", "IsSpecial")
    version = editable_table(database, catalog, key, columns)
    names = [str(900001 + i) for i in range(len(model.points))]
    rows = [
        (name, f"{x:.9f}", f"{y:.9f}", f"{z:.9f}", "Yes")
        for name, (x, y, z) in zip(names, model.points, strict=True)
    ]
    import_tables(database, [TableEdit(key, version, columns, rows)])
    for name, expected in zip(names, model.points, strict=True):
        actual = outputs(sap.PointObj.GetCoordCartesian(name, 0.0, 0.0, 0.0), 3, f"verify joint {name}")
        if not all(isclose(a, b, abs_tol=1e-6) for a, b in zip(actual, expected, strict=True)):
            raise EtabsError(f"ETABS imported joint {name} at the wrong position.")
    return names


def create_frames(sap: Any, model: Model, names: list[str], database: Any, catalog: dict[str, int]) -> list[str]:
    columns = ("UniqueName", "UniquePtI", "UniquePtJ")
    groups: dict[str, list[tuple[str, ...]]] = {}
    frames = []
    for index, member in enumerate(model.members):
        x1, y1, z1 = model.points[member.start]
        x2, y2, z2 = model.points[member.end]
        if isclose(x1, x2, abs_tol=1e-8) and isclose(y1, y2, abs_tol=1e-8):
            kind = "Column"
        else:
            kind = "Beam" if isclose(z1, z2, abs_tol=1e-8) else "Brace"
        name = str(910001 + index)
        frames.append(name)
        groups.setdefault(f"{kind} Object Connectivity", []).append(
            (name, names[member.start], names[member.end]),
        )

    # ETABS rejects the frame tables when they are applied together (status 1, empty log), so each gets its own import.
    for key, rows in groups.items():
        import_tables(database, [TableEdit(key, editable_table(database, catalog, key, columns), columns, rows)])

    for frame, member in zip(frames, model.members, strict=True):
        section = SECTION_IDS[member.kind]
        actual = outputs(sap.FrameObj.GetPoints(frame, "", ""), 2, f"verify member {frame}")
        expected = [names[member.start], names[member.end]]
        if set(actual) != set(expected):  # ETABS may swap I and J so the lower joint comes first
            raise EtabsError(f"ETABS imported the wrong endpoints for member {frame}: expected {expected}, got {actual}.")
        check(sap.FrameObj.SetSection(frame, section), f"set the section on member {frame}")
        assigned, _ = outputs(sap.FrameObj.GetSection(frame, "", ""), 2, f"verify the section on member {frame}")
        if assigned != section:
            raise EtabsError(f"ETABS imported the wrong section for member {frame}.")
    return frames


def build_geometry(sap: Any, model: Model) -> tuple[list[str], list[str], str]:
    database = sap.DatabaseTables
    count, keys, _, import_types, _ = outputs(database.GetAllTables(0, [], [], [], []), 5, "read editable tables")
    if count != len(keys) or count != len(import_types):
        raise EtabsError("ETABS returned an incomplete table catalog.")
    catalog = dict(zip(keys, import_types, strict=True))
    names = create_points(sap, model, database, catalog)
    frames = create_frames(sap, model, names, database, catalog)
    if len(set(names)) != len(model.points) or not all(names) or int(sap.PointObj.Count()) != len(names):
        raise EtabsError("ETABS merged or omitted joints.")
    if len(set(frames)) != len(model.members) or not all(frames) or int(sap.FrameObj.Count()) != len(frames):
        raise EtabsError("ETABS merged or omitted members.")
    return names, frames, f"Database import · {len(names)} joints and {len(frames)} members"


def build_in_etabs(model_api: Any, model: Model) -> tuple[list[str], str]:
    check(model_api.SetPresentUnits(6), "set kN and m units")
    material = model_api.PropMaterial
    check(material.SetMaterial(STEEL, 1), "define steel")
    check(material.SetMPIsotropic(STEEL, 200_000_000, 0.3, 1.2e-5), "set steel stiffness")
    check(material.SetWeightAndMass(STEEL, 1, 77.0), "set steel unit weight")
    check(model_api.GroupDef.SetGroup(JOINT_GROUP), "define the joint group")

    sections = model_api.PropFrame
    for section_id, section in {SECTION_IDS[kind]: section for kind, section in SECTIONS.items()}.items():
        if section.flange:
            result = sections.SetISection(
                section_id, STEEL, section.depth, section.width, section.flange,
                section.web, section.width, section.flange,
            )
        else:
            result = sections.SetPipe(section_id, STEEL, section.depth, section.web)
        check(result, f"define {section.name}")

    for pattern, kind, self_weight in PATTERNS:
        check(model_api.LoadPatterns.Add(pattern, kind, self_weight, True), f"define {pattern}")

    points = model_api.PointObj
    names, frames, method = build_geometry(model_api, model)

    for name in names:
        check(points.SetGroupAssign(name, JOINT_GROUP), "group the joint")
    for index in model.bases:
        check(points.SetRestraint(names[index], [True] * 6), "fix the column base")
    for index, member in enumerate(model.members):
        frame = frames[index]
        if member.kind in {"web", "roof_brace", "side_brace"}:
            check(model_api.FrameObj.SetReleases(
                frame, [False, False, False, True, True, True],
                [False, False, False, False, True, True], [0.0] * 6, [0.0] * 6,
            ), f"release the CHS ends on member {index + 1}")

    for load in model.roof:
        for pattern, force in (
            ("SHED-DEAD", load["dead"]), ("SHED-ROOF-LIVE", load["live"]), ("SHED-WIND", load["uplift"]),
        ):
            if force:
                check(points.SetLoadForce(
                    names[load["joint"]], pattern, [0.0, 0.0, force, 0.0, 0.0, 0.0], True,
                ), f"apply {pattern} roof load")
    for load in model.wind:
        line = load["line"]
        check(model_api.FrameObj.SetLoadDistributed(
            frames[load["member"]], "SHED-WIND", 1, 4, 0.0, 1.0, line, line, "Global", True, True,
        ), "apply side wall wind")
    for combo, patterns in COMBINATIONS.items():
        check(model_api.RespCombo.Add(combo, 0), f"define {combo}")
        for pattern in patterns:
            check(model_api.RespCombo.SetCaseList(combo, 0, pattern, 1.0), f"add {pattern} to {combo}")
    return names, method


def translations(raw: Any, names: list[str], combo: str) -> list[Point]:
    count, objects, elements, cases, types, steps, ux, uy, uz, rx, ry, rz = outputs(
        raw, 12, f"read displacements for {combo}",
    )
    arrays = (objects, elements, cases, types, steps, ux, uy, uz, rx, ry, rz)
    if int(count) != len(names) or any(len(array) != int(count) for array in arrays):
        raise EtabsError(f"ETABS returned incomplete joint displacements for {combo}.")
    if set(objects) != set(names) or set(cases) != {combo}:
        raise EtabsError(f"ETABS returned unexpected joints or load cases for {combo}.")
    by_name = {name: (float(x), float(y), float(z)) for name, x, y, z in zip(objects, ux, uy, uz, strict=True)}
    values = [by_name[name] for name in names]
    if any(not isfinite(value) for vector in values for value in vector):
        raise EtabsError(f"ETABS returned non-finite displacements for {combo}.")
    return values


def base_reaction(sap: Any, combo: str) -> Point:
    raw = sap.Results.BaseReact(0, [], [], [], [], [], [], [], [], [], 0.0, 0.0, 0.0)
    count, cases, _, _, fx, fy, fz, *_ = outputs(raw, 13, f"read the base reaction for {combo}")
    if int(count) != 1 or list(cases) != [combo] or any(len(items) != 1 for items in (fx, fy, fz)):
        raise EtabsError(f"ETABS returned an unexpected base reaction for {combo}.")
    reaction = (float(fx[0]), float(fy[0]), float(fz[0]))
    if not all(isfinite(component) for component in reaction):
        raise EtabsError(f"ETABS returned a non-finite base reaction for {combo}.")
    return reaction


def analyze(sap: Any, shed: Shed, model: Model, path: Path) -> tuple[dict[str, ComboResult], str]:
    names, method = build_in_etabs(sap, model)
    check(sap.File.Save(str(path)), "save the model for analysis")
    check(sap.Analyze.SetRunCaseFlag("", False, True), "clear the run selection")
    for pattern, _, _ in PATTERNS:
        check(sap.Analyze.SetRunCaseFlag(pattern, True, False), f"select {pattern}")
    check(sap.Analyze.RunAnalysis(), "run the analysis")
    count, case_names, case_statuses = outputs(sap.Analyze.GetCaseStatus(0, [], []), 3, "read the run status")
    if int(count) != len(case_names) or int(count) != len(case_statuses):
        raise EtabsError("ETABS returned incomplete analysis status.")
    completed = dict(zip(case_names, case_statuses, strict=True))
    if any(completed.get(pattern) != 4 for pattern, _, _ in PATTERNS):
        raise EtabsError("ETABS did not finish every load case. Review its analysis log.")

    results: dict[str, ComboResult] = {}
    for combo in COMBINATIONS:
        check(sap.Results.Setup.DeselectAllCasesAndCombosForOutput(), "clear the result selection")
        check(sap.Results.Setup.SetComboSelectedForOutput(combo, True), f"select {combo}")
        displacement = translations(
            sap.Results.JointDispl(JOINT_GROUP, 2, 0, *[[] for _ in range(11)]), names, combo,
        )
        reaction = base_reaction(sap, combo)
        applied_x = load_totals(shed)["wall_wind_kn"] if combo == WIND_COMBO else 0.0
        if not isclose(reaction[0], -applied_x, rel_tol=0.02, abs_tol=0.02):
            raise EtabsError(f"The base reaction for {combo} differs from the applied horizontal load.")
        results[combo] = {
            "translations_m": displacement,
            "max_displacement_mm": 1000 * max(sum(value**2 for value in vector)**0.5 for vector in displacement),
            "base_reaction_kn": reaction,
        }
    return results, method


class EtabsUnavailable(RuntimeError):
    pass


def platform_note() -> str | None:
    if sys.platform != "win32":
        return "Preview mode · ETABS runs on Windows"
    try:
        import comtypes.client
    except (ImportError, OSError):
        return "Install the dependencies from requirements.txt to run ETABS."
    return None


def initialize(sap: Any, shed: Shed) -> None:
    check(sap.InitializeNewModel(6), "initialize a new model")
    check(sap.File.NewGridOnly(2, shed.rise, shed.eave_height, 2, shed.bays + 1, shed.span, shed.bay_length),
          "create the shed grid")


def run_shed(shed: Shed, runs: Path) -> tuple[str, dict[str, ComboResult], str]:
    if note := platform_note():
        raise EtabsUnavailable(note)

    import comtypes
    from comtypes import client

    comtypes.CoInitialize()
    helper = application = model_api = None
    try:
        try:
            helper = client.CreateObject("ETABSv1.Helper")
            from comtypes.gen import ETABSv1

            helper = helper.QueryInterface(ETABSv1.cHelper)
            application = helper.CreateObjectProgID("CSI.ETABS.API.ETABSObject")
            check(application.ApplicationStart(), "start ETABS")
        except (OSError, AttributeError, ImportError, comtypes.COMError, EtabsError) as error:
            raise EtabsUnavailable("Could not start ETABS. Check the Windows installation and license.") from error

        model_api = application.SapModel
        initialize(model_api, shed)
        run_id = uuid4().hex
        folder = runs.resolve() / run_id
        folder.mkdir(parents=True)
        path = folder / "steel-shed.edb"
        result, method = analyze(model_api, shed, build_model(shed), path)
        if not path.is_file():
            raise EtabsError("ETABS did not save the model file.")
        (folder / "complete").touch()
        return run_id, result, method
    except comtypes.COMError as error:
        raise EtabsError(f"ETABS stopped during the analysis. See the ETABS messages. {error}") from error
    finally:
        if application is not None:
            try:
                check(application.ApplicationExit(False), "close the demo instance")
            except Exception:
                logging.getLogger(__name__).exception("ETABS did not close the demo instance.")
        model_api = application = helper = None
        comtypes.CoUninitialize()

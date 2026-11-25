from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from etabs_model import EtabsError, PATTERNS, analyze, build_geometry, translations
from shed import COMBO_LABELS, ROOF_COMBO, WIND_COMBO, Shed, build_model, load_totals
from viewer import model_payload


class ShedTests(TestCase):
    def test_geometry_and_last_bay_bracing(self):
        shed = Shed(bays=4, panels=4)
        model = build_model(shed)
        self.assertEqual(len(set(model.points)), len(model.points))
        connections = {frozenset((member.start, member.end)) for member in model.members}
        self.assertEqual(len(connections), len(model.members))
        self.assertTrue(all(len(endpoints) == 2 for endpoints in connections))
        self.assertEqual(len(model.bases), 2 * (shed.bays + 1))
        for kind in ("roof_brace", "side_brace"):
            braces = [member for member in model.members if member.kind == kind]
            self.assertEqual(len(braces), 4)
            for brace in braces:
                self.assertEqual(
                    {model.points[brace.start][1], model.points[brace.end][1]},
                    {shed.length - shed.bay_length, shed.length},
                )

    def test_tributary_load_totals_and_wind_direction(self):
        for side in ("+X", "-X"):
            shed = Shed(span=18, bays=5, panels=4, wind_side=side)
            model = build_model(shed)
            expected = load_totals(shed)
            for load in ("dead", "live"):
                self.assertAlmostEqual(sum(row[load] for row in model.roof), -expected[f"roof_{load}_kn"])
            self.assertAlmostEqual(sum(row["uplift"] for row in model.roof), expected["roof_uplift_kn"])
            self.assertAlmostEqual(sum(row["line"] for row in model.wind) * shed.eave_height, expected["wall_wind_kn"])
            windward_x = 0 if side == "+X" else shed.span
            for row in model.wind:
                self.assertEqual(model.points[model.members[row["member"]].start][0], windward_x)

    def test_invalid_numeric_inputs(self):
        for key, bad in (("span", float("nan")), ("bays", 2.5), ("roof_live", -1), ("panels", True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Shed.from_values(asdict(Shed()) | {key: bad})

    def test_roof_combination_is_first_and_default(self):
        payload = model_payload(Shed())
        self.assertEqual(payload["roof_combo"], ROOF_COMBO)
        self.assertEqual(list(COMBO_LABELS), [ROOF_COMBO, WIND_COMBO])
        self.assertIsNone(payload["combinations"])


class EtabsTests(TestCase):
    def setUp(self):
        self.shed = Shed(bays=1, panels=2)
        self.model = build_model(self.shed)
        self.points = {}
        self.frames = {}
        self.sections = {}
        self.combo = ROOF_COMBO
        self.sap = SimpleNamespace(SetPresentUnits=Mock(return_value=0))
        for group in ("PropMaterial", "PropFrame", "PointObj", "FrameObj", "GroupDef",
                      "LoadPatterns", "RespCombo", "File", "Analyze", "Results", "DatabaseTables"):
            setattr(self.sap, group, Mock(return_value=0))
        for group, methods in (
            ("PropMaterial", ("SetMaterial", "SetMPIsotropic", "SetWeightAndMass")),
            ("PropFrame", ("SetISection", "SetPipe")),
            ("PointObj", ("SetGroupAssign", "SetRestraint", "SetLoadForce")),
            ("FrameObj", ("SetReleases", "SetLoadDistributed")),
            ("GroupDef", ("SetGroup",)), ("LoadPatterns", ("Add",)),
            ("RespCombo", ("Add", "SetCaseList")), ("File", ("Save",)),
            ("Analyze", ("SetRunCaseFlag", "RunAnalysis")),
        ):
            for method in methods:
                getattr(getattr(self.sap, group), method).return_value = 0
        self.sap.PointObj.AddCartesian.side_effect = self.add_point
        self.sap.PointObj.Count.side_effect = lambda: len(self.points)
        self.sap.PointObj.GetCoordCartesian.side_effect = lambda name, *_: [*self.points[name], 0]
        self.sap.FrameObj.AddByPoint.side_effect = self.add_frame
        self.sap.FrameObj.Count.side_effect = lambda: len(self.frames)
        self.sap.FrameObj.GetPoints.side_effect = lambda name, *_: [*self.frames[name], 0]
        self.sap.FrameObj.SetSection.side_effect = self.set_section
        self.sap.FrameObj.GetSection.side_effect = lambda name, *_: [self.sections[name], "", 0]
        self.sap.Analyze.GetCaseStatus.return_value = [3, [pattern[0] for pattern in PATTERNS], [4, 4, 4], 0]
        self.sap.Results.Setup.DeselectAllCasesAndCombosForOutput.return_value = 0
        self.sap.Results.Setup.SetComboSelectedForOutput.side_effect = self.select_combo
        self.sap.Results.JointDispl.side_effect = self.joint_displacements
        self.sap.Results.BaseReact.side_effect = self.base_reaction
        self.mock_database()

    def mock_database(self):
        database = self.sap.DatabaseTables
        point_key = "Point Object Connectivity"
        frame_keys = ("Beam Object Connectivity", "Column Object Connectivity", "Brace Object Connectivity")
        keys = [point_key, *frame_keys]
        database.GetAllTables.return_value = [4, keys, keys, [2] * 4, [True] * 4, 0]
        point_fields = ["UniqueName", "Story", "X", "Y", "Z", "IsSpecial", "GUID"]
        frame_fields = ["UniqueName", "Story", "UniquePtI", "UniquePtJ"]
        metadata = {point_key: point_fields, **{key: frame_fields for key in frame_keys}}
        read_only = {"Story", "GUID"}  # ETABS derives Story from the geometry
        database.GetAllFieldsInTable.side_effect = lambda key, *_: [
            1, len(metadata[key]), metadata[key], [], [], [],
            [field not in read_only for field in metadata[key]], 0,
        ]
        database.GetTableForEditingArray.return_value = [1, [], 0, [], 0]
        database.CancelTableEditing.return_value = 0
        database.ApplyEditedTables.return_value = [0, 0, 0, 0, "", 0]

        def import_rows(key, version, columns, count, flat):
            self.assertEqual(version, 1)
            self.assertNotIn("GUID", columns)
            self.assertNotIn("Story", columns)
            self.assertEqual(len(flat), len(columns) * count)
            for offset in range(0, len(flat), len(columns)):
                row = dict(zip(columns, flat[offset:offset + len(columns)], strict=True))
                name = row["UniqueName"]
                if key == point_key:
                    self.points[name] = tuple(float(row[axis]) for axis in "XYZ")
                else:
                    self.frames[name] = (row["UniquePtI"], row["UniquePtJ"])
            return [version, columns, flat, 0]

        database.SetTableForEditingArray.side_effect = import_rows

    def add_point(self, x, y, z, _, name):
        self.points[name] = (x, y, z)
        return [name, 0]

    def add_frame(self, start, end, _, section, name):
        self.frames[name] = (start, end)
        self.sections[name] = section
        return [name, 0]

    def set_section(self, name, section):
        self.sections[name] = section
        return 0

    def select_combo(self, name, selected):
        self.combo = name
        return 0

    def joint_displacements(self, *_):
        names = list(reversed(self.points))
        n = len(names)
        zero = [0.0] * n
        dz = [0.0 if self.points[name][2] == 0 else -0.002 for name in names]
        return [n, names, names, [self.combo] * n, [""] * n, zero, zero, zero, dz, zero, zero, zero, 0]

    def base_reaction(self, *_):
        rx = -load_totals(self.shed)["wall_wind_kn"] if self.combo == WIND_COMBO else 0
        return [1, [self.combo], [""], [0], [rx], [0], [100], [0], [0], [0], 0, 0, 0, 0]

    def test_typed_calls_apply_loads_and_read_both_combinations(self):
        results, method = analyze(self.sap, self.shed, self.model, Path("fixture.edb"))
        self.assertTrue(method.startswith("Database import"))
        self.assertEqual(list(results), [ROOF_COMBO, WIND_COMBO])
        self.assertEqual(results[ROOF_COMBO]["max_displacement_mm"], 2)
        self.assertEqual(results[ROOF_COMBO]["translations_m"][0], (0, 0, 0))
        self.assertAlmostEqual(self.sap.FrameObj.SetLoadDistributed.call_args.args[6], 1.875)
        self.assertEqual(self.sap.Results.JointDispl.call_count, 2)

    def test_displacement_read_rejects_missing_joints(self):
        self.points["J1"] = (1, 1, 0)
        with self.assertRaises(EtabsError):
            translations(self.joint_displacements(), ["J1", "J2"], ROOF_COMBO)

    def test_database_geometry_import_and_readback(self):
        names, frames, method = build_geometry(self.sap, self.model)
        self.assertEqual(method, f"Database import · {len(self.model.points)} joints and {len(self.model.members)} members")
        self.assertEqual(len(names), len(self.model.points))
        self.assertEqual(len(frames), len(self.model.members))
        self.assertEqual(self.sap.DatabaseTables.ApplyEditedTables.call_count, 4)  # points, then one import per frame table
        self.sap.PointObj.AddCartesian.assert_not_called()
        self.sap.FrameObj.AddByPoint.assert_not_called()

    def test_unimportable_table_fails_without_direct_api_fallback(self):
        self.sap.DatabaseTables.GetAllTables.return_value = [1, ["Point Object Connectivity"], ["x"], [0], [True], 0]
        with self.assertRaises(EtabsError):
            build_geometry(self.sap, self.model)
        self.sap.PointObj.AddCartesian.assert_not_called()

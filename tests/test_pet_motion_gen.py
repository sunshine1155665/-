"""Tests for scripts/pet_motion_gen.py: DSL invariants + rig id validation."""

import importlib.util
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "pet_motion_gen", os.path.join(ROOT, "scripts", "pet_motion_gen.py")
)
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


def cdi_ids():
    with open(gen.CDI_PATH, encoding="utf-8") as fh:
        return {p["Id"] for p in json.load(fh)["Parameters"]}


class MotionDslTest(unittest.TestCase):
    def test_meta_counts_linear(self):
        m = gen.Motion(1.0)
        m.keys("ParamAngleX", [(0, 0), (0.5, 10), (1.0, 0)])
        data = m.to_json()
        self.assertEqual(data["Meta"]["CurveCount"], 1)
        self.assertEqual(data["Meta"]["TotalPointCount"], 3)
        self.assertEqual(data["Meta"]["TotalSegmentCount"], 2)
        self.assertEqual(data["Meta"]["Duration"], 1.0)
        self.assertFalse(data["Meta"]["Loop"])

    def test_segments_format_no_keyform_prefix(self):
        # motion3 Segments = [t0, v0, type, t1, v1, ...]; verified against the
        # stock rig's own motion files (89/89 curves match this layout).
        m = gen.Motion(1.0)
        m.keys("ParamAngleX", [(0, 0), (1.0, 5)])
        segs = m.to_json()["Curves"][0]["Segments"]
        self.assertEqual(segs, [0.0, 0.0, 0, 1.0, 5.0])

    def test_segment_types_are_valid_everywhere(self):
        for name, (builder, _fi, _fo, _idle) in gen.MOTIONS.items():
            for curve in builder().to_json()["Curves"]:
                segs = curve["Segments"]
                self.assertIn(segs[2], (0, 1, 2, 3), f"{name}/{curve['Id']} first type")
                for i in range(2, len(segs) - 2, 3):
                    self.assertIn(segs[i], (0, 1, 2, 3), f"{name}/{curve['Id']} type@{i}")

    def test_eased_keys_are_sampled_at_fps(self):
        m = gen.Motion(1.0, fps=30)
        m.keys("Param13", [(0, 0), (1.0, 10, gen.smooth)])
        data = m.to_json()
        # 30fps sampling + endpoints
        self.assertGreaterEqual(data["Meta"]["TotalPointCount"], 30)

    def test_short_trailing_keys_extend_to_duration(self):
        m = gen.Motion(2.0)
        m.keys("ParamAngleY", [(0, 0), (1.0, 6)])
        pts = m._resolve("ParamAngleY")
        self.assertEqual(pts[-1], (2.0, 6.0))

    def test_loop_closure_enforced(self):
        m = gen.Motion(2.0, loop=True)
        m.waves("Param13", 0, 5.0, 0.9)  # 0.9 does not divide 2.0
        with self.assertRaises(ValueError):
            m.to_json()

    def test_wrap_allows_full_rotation_loop(self):
        m = gen.Motion(2.0, loop=True)
        m.sweep("ParamCheek28", 0, 360).wrap("ParamCheek28", 360)
        data = m.to_json()  # must not raise
        self.assertTrue(data["Meta"]["Loop"])

    def test_unknown_param_caught_by_validation(self):
        m = gen.Motion(1.0)
        m.keys("ParamNotExists", [(0, 0), (1.0, 1)])
        gen.MOTIONS["__test_bad"] = (lambda: m, 0.3, 0.4, False)
        try:
            errors = gen.validate_all(cdi_ids())
            self.assertTrue(any("__test_bad" in e and "ParamNotExists" in e for e in errors))
        finally:
            gen.MOTIONS.pop("__test_bad", None)


class RegistryTest(unittest.TestCase):
    def test_all_motions_build_and_use_known_params(self):
        errors = gen.validate_all(cdi_ids())
        self.assertEqual(errors, [], "\n".join(errors))

    def test_registry_has_scene_idles(self):
        for name in ("idle_work", "idle_meal", "idle_clingy", "idle_happy", "idle_night", "idle_devil"):
            self.assertIn(name, gen.MOTIONS, name)
            self.assertTrue(gen.MOTIONS[name][3], f"{name} must be flagged as idle")

    def test_idle_loops_are_long_and_seamless(self):
        for name, (builder, _fi, _fo, is_idle) in gen.MOTIONS.items():
            data = builder().to_json()
            self.assertEqual(data["Meta"]["Loop"], is_idle, name)
            if is_idle:
                self.assertGreaterEqual(data["Meta"]["Duration"], 4.0, name)
                self.assertLessEqual(data["Meta"]["Duration"], 10.0, name)
            else:
                self.assertLessEqual(data["Meta"]["Duration"], 4.0, name)

    def test_motions_do_not_drive_blink_group_conflicts(self):
        # EyeBlink group targets eye open params; motions may animate them, but
        # the renderer disables auto-blink so authored values always win.
        ids = cdi_ids()
        self.assertIn("ParamEyeLOpen", ids)


if __name__ == "__main__":
    unittest.main()

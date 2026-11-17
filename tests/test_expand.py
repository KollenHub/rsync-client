from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from rsync_client.glob_expand import (
    Expansion,
    build_group_specs,
    expand_local,
    expand_segments,
)
from rsync_client.models import TaskSpec


def make_tree(root: Path):
    for folder in ("Pose", "t1/Pose", "t1/inner/Pose", "t2/x/Pose", "other"):
        (root / folder).mkdir(parents=True)
    (root / "t1" / "Pose" / "a.info").write_text("a")
    (root / "t1" / "b.txt").write_text("b")
    (root / "Pose" / "zero.info").write_text("z")
    (root / "t3").mkdir()
    (root / "t3" / "2026-01-01.log").write_text("l")
    (root / "t3" / "2027-02-02.log").write_text("l")


class SegmentTests(unittest.TestCase):
    """One shared matcher core: local scandir and remote SFTP feed the same walk."""

    TREE: ClassVar[dict[str, list[tuple[str, bool]]]] = {
        "": [("Pose", True), ("t1", True), ("t2", True), ("t3", True)],
        "/Pose": [("zero.info", False)],
        "/t1": [("Pose", True), ("inner", True), ("b.txt", False)],
        "/t1/Pose": [("a.info", False)],
        "/t1/inner": [("Pose", True)],
        "/t1/inner/Pose": [],
        "/t2": [("x", True)],
        "/t2/x": [("Pose", True)],
        "/t2/x/Pose": [],
        "/t3": [("2026-01-01.log", False), ("2027-02-02.log", False)],
    }

    def expand(self, pattern):
        def list_children(folder):
            return self.TREE[folder]
        return expand_segments("", pattern, list_children)

    def test_single_level(self):
        self.assertEqual(self.expand("*/Pose"), ["/t1/Pose"])

    def test_recursive_includes_zero_level(self):
        self.assertEqual(self.expand("**/Pose"), ["/Pose", "/t1/Pose", "/t1/inner/Pose", "/t2/x/Pose"])

    def test_question_and_class(self):
        self.assertEqual(sorted(self.expand("t?/Pose")), ["/t1/Pose"])
        self.assertEqual(sorted(self.expand("t[123]/*.log")), ["/t3/2026-01-01.log", "/t3/2027-02-02.log"])

    def test_tail_slash_matches_directories_only(self):
        self.assertEqual(sorted(self.expand("*/inner/")), ["/t1/inner"])

    def test_tail_star_matches_files_too(self):
        self.assertEqual(self.expand("t1/Pose/*"), ["/t1/Pose/a.info"])

    def test_no_match(self):
        self.assertEqual(self.expand("zz/*"), [])


class LocalExpandTests(unittest.TestCase):
    def test_expand_local_and_group_specs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "lvio"
            for folder in ("a/task1/Pose", "b/task2/Pose"):
                (root / folder).mkdir(parents=True)
            (root / "a" / "task1" / "Pose" / "x.info").write_text("x")
            (root / "loose.txt").write_text("l")

            expansion = expand_local(f"{root}/*/*/Pose")
            self.assertEqual(sorted(expansion.matches), sorted([f"{root}/a/task1/Pose", f"{root}/b/task2/Pose"]))
            self.assertEqual(expansion.relative(f"{root}/b/task2/Pose"), "b/task2/Pose")

            specs = build_group_specs(expansion, str(Path(td) / "out"), direction="local", local=True,
                                      group_name="Pose 组")
            self.assertEqual(len(specs), 2)
            by_name = {spec.name: spec for spec in specs}
            self.assertEqual(set(by_name), {"Pose 组 / a/task1/Pose", "Pose 组 / b/task2/Pose"})
            first = by_name["Pose 组 / a/task1/Pose"]
            # The relative tree lands under the destination root: out/a/task1/Pose.
            self.assertEqual(first.source, f"{root}/a/task1/Pose")
            self.assertEqual(first.destination, str(Path(td) / "out" / "a" / "task1"))
            self.assertEqual(first.group_id, specs[1].group_id)
            self.assertTrue(first.group_id)
            for spec in specs:
                spec.validate()

    def test_missing_prefix_rejected(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
            expand_local(f"{Path(td) / 'missing'}/*/Pose")

    def test_no_matches_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a").mkdir()
            with self.assertRaises(ValueError):
                expand_local(f"{td}/zz*/*")

    def test_limit_enforced(self):
        import rsync_client.glob_expand as ge
        tree = {"": [(f"d{i}", True) for i in range(5)]}
        original = ge.MAX_MATCHES
        ge.MAX_MATCHES = 2
        try:
            with self.assertRaises(ValueError):
                expand_segments("", "d*", lambda folder: tree.get(folder, []))
        finally:
            ge.MAX_MATCHES = original

    def test_download_direction_keeps_remote_source(self):
        expansion = Expansion("/srv/lvio", "*/*/Pose", ("/srv/lvio/a/1/Pose",))
        specs = build_group_specs(expansion, "/mnt/d/out", direction="download", local=False,
                                  group_name="handan")
        spec = specs[0]
        self.assertEqual(spec.source, "/srv/lvio/a/1/Pose")
        self.assertEqual(spec.destination, "/mnt/d/out/a/1")
        # Remote tasks only validate after the dialog binds a server profile.
        replace(spec, host="srv", username="u").validate(check_paths=False)

class GroupFieldTests(unittest.TestCase):
    def test_group_fields_validated(self):
        with self.assertRaises(TypeError):
            TaskSpec("bad", "/tmp/x", "/tmp/out", group_id=1).validate(check_paths=False)
        with self.assertRaises(TypeError):
            TaskSpec("bad", "/tmp/x", "/tmp/out", group_name=[]).validate(check_paths=False)
        TaskSpec("ok", "/tmp/x", "/tmp/out", group_id="g1", group_name="组").validate(check_paths=False)

    def test_group_fields_round_trip(self):
        spec = TaskSpec("ok", "/tmp/x", "/tmp/out", direction="upload", host="s", username="u",
                        group_id="g1", group_name="组")
        data = spec.public_dict()
        self.assertEqual(data["group_id"], "g1")
        restored = TaskSpec.from_dict(data)
        self.assertEqual((restored.group_id, restored.group_name), ("g1", "组"))


if __name__ == "__main__":
    unittest.main()

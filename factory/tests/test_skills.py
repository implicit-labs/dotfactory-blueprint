import tempfile
import unittest
from pathlib import Path

from dotfactory import SkillResolutionError, SkillResolver
from dotfactory.skills import verify_resolved_skill


class SkillResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, name="proof-skill"):
        root = self.root / name
        root.mkdir()
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Protocol proof.\n---\n\nDo the proof.\n",
            encoding="utf-8",
        )
        return root

    def test_missing_declared_skill_fails_by_name(self):
        with self.assertRaisesRegex(
            SkillResolutionError, "not installed: missing-skill"
        ) as caught:
            SkillResolver(self.root).resolve(["missing-skill"])
        self.assertEqual(("missing-skill",), caught.exception.missing)

    def test_resolution_hashes_the_complete_installed_package(self):
        skill = self.install()
        (skill / "references").mkdir()
        (skill / "references" / "contract.md").write_text(
            "first contract\n", encoding="utf-8",
        )
        first = SkillResolver(self.root).resolve(["proof-skill"])[0]
        self.assertEqual(2, first.file_count)
        self.assertEqual(64, len(first.content_hash))

        (skill / "references" / "contract.md").write_text(
            "changed contract\n", encoding="utf-8",
        )
        second = SkillResolver(self.root).resolve(["proof-skill"])[0]
        self.assertNotEqual(first.content_hash, second.content_hash)
        with self.assertRaisesRegex(SkillResolutionError, "changed after preparation"):
            verify_resolved_skill(first)

    def test_entrypoint_name_must_match_the_declared_name(self):
        root = self.root / "proof-skill"
        root.mkdir()
        (root / "SKILL.md").write_text(
            "---\nname: different-skill\ndescription: Wrong.\n---\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SkillResolutionError, "mismatched SKILL.md name"):
            SkillResolver(self.root).resolve(["proof-skill"])


if __name__ == "__main__":
    unittest.main()

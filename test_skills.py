import unittest

from skills import parse_github_skill_url


class SkillUrlTests(unittest.TestCase):
    def test_accepts_repository_root_and_tree_skill_paths(self):
        self.assertEqual(
            parse_github_skill_url("https://github.com/owner/repo"),
            ("owner/repo", ""),
        )
        self.assertEqual(
            parse_github_skill_url("https://github.com/owner/repo/tree/main/skills/card"),
            ("owner/repo", "skills/card"),
        )

    def test_rejects_non_github_and_ambiguous_paths(self):
        for value in (
            "https://example.com/owner/repo",
            "http://github.com/owner/repo",
            "https://github.com/owner/repo/blob/main/SKILL.md",
            "https://github.com/owner/repo?tab=readme",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_github_skill_url(value)


if __name__ == "__main__":
    unittest.main()

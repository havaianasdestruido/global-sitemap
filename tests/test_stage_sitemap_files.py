"""Tests for scripts/stage_sitemap_files.sh.

Each test builds a throwaway git repository, runs the real script in it, and
checks what ended up staged. This is the logic that broke the workflow twice
(see the script's header), so it is exercised as a subprocess rather than
reimplemented in Python.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "stage_sitemap_files.sh"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


class StageSitemapFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", ".")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "README.md").write_text("unrelated\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def stage(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-e", str(SCRIPT)],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )

    def staged(self) -> list[str]:
        out = git(self.repo, "diff", "--cached", "--name-status").stdout
        return sorted(line for line in out.splitlines() if line.strip())

    def commit_all(self, **files: str) -> None:
        for name, content in files.items():
            (self.repo / name.replace("__", ".")).write_text(content, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "seed sitemap files")

    def test_no_tracked_sitemap_files_succeeds(self) -> None:
        """The regression: nothing tracked must not exit 128."""
        (self.repo / "sitemap.xml").write_text("<urlset/>\n", encoding="utf-8")
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("did not match any files", result.stderr)
        self.assertEqual(self.staged(), ["A\tsitemap.xml"])

    def test_deletes_legacy_text_sitemap(self) -> None:
        self.commit_all(sitemap__txt="https://example.com/\n")
        (self.repo / "sitemap.txt").unlink()
        (self.repo / "sitemap.xml").write_text("<urlset/>\n", encoding="utf-8")
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.staged(), ["A\tsitemap.xml", "D\tsitemap.txt"])

    def test_index_parts_and_stale_part(self) -> None:
        self.commit_all(sitemap__xml="<urlset/>\n")
        (self.repo / "sitemap-1.xml").write_text("a\n", encoding="utf-8")
        (self.repo / "sitemap-2.xml").write_text("b\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "two parts")

        # New run: index plus one part; sitemap-2.xml is gone.
        (self.repo / "sitemap.xml").write_text("<sitemapindex/>\n", encoding="utf-8")
        (self.repo / "sitemap-1.xml").write_text("a2\n", encoding="utf-8")
        (self.repo / "sitemap-2.xml").unlink()

        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.staged(),
            ["D\tsitemap-2.xml", "M\tsitemap-1.xml", "M\tsitemap.xml"],
        )

    def test_unchanged_tree_stages_nothing(self) -> None:
        self.commit_all(sitemap__xml="<urlset/>\n", robots__txt="User-agent: *\n")
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.staged(), [])

    def test_reports_no_changes_after_staging(self) -> None:
        """The workflow's `git diff --cached --quiet` check must be clean."""
        self.commit_all(sitemap__xml="<urlset/>\n")
        self.stage()
        quiet = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=self.repo
        )
        self.assertEqual(quiet.returncode, 0)

    def test_rejects_untracked_non_sitemap_files(self) -> None:
        """Only sitemap output is committed; scratch files stay out."""
        (self.repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
        self.stage()
        self.assertNotIn("notes.txt", str(self.staged()))

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_script_is_shell_lint_clean(self) -> None:
        if not shutil.which("bash"):
            self.skipTest("bash not available")
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

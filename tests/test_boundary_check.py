import subprocess
import tempfile
import unittest
from pathlib import Path

from boundary_check import BoundaryCheck


class BoundaryCheckTests(unittest.TestCase):
    def test_denylist_case_insensitive(self):
        for term in ("NATAegisFlow", "studio-offers", "TURGON", "penguin"):
            self.assertTrue(BoundaryCheck.scan_text("README.md", f"see {term} notes"), term)

    def test_exempt_file_skips_denylist_only(self):
        self.assertEqual(BoundaryCheck.scan_text("scripts/boundary_check.py", "turgon"), [])
        self.assertTrue(BoundaryCheck.scan_text("scripts/boundary_check.py", "x@corp.com"))
        self.assertTrue(BoundaryCheck.scan_text("README.md", "x@corp.com"))
        self.assertEqual(BoundaryCheck.scan_text("tests/test_boundary_check.py", "penguin x@corp.com"), [])

    def test_template_hosts_skipped_but_concrete_hosts_checked(self):
        self.assertEqual(BoundaryCheck.scan_text("a.py", 'f"https://www.{host}"'), [])
        self.assertTrue(BoundaryCheck.scan_text("a.py", 'f"https://www.corp.com/{path}"'))

    def test_urls_and_emails(self):
        self.assertEqual(BoundaryCheck.scan_text("a.md", "https://www.quorvane.example/about and ops@quorvane.example"), [])
        self.assertEqual(BoundaryCheck.scan_text("a.md", "https://api.openai.invalid/v1 x@y.invalid"), [])
        self.assertTrue(BoundaryCheck.scan_text("a.md", "https://github.com/x"))
        self.assertTrue(BoundaryCheck.scan_text("a.md", "mail me at someone@gmail.com"))
        self.assertTrue(BoundaryCheck.scan_text("a.md", "http://example.com"))  # .com is not reserved-TLD .example

    def test_bare_domains_without_scheme_are_not_flagged(self):
        self.assertEqual(BoundaryCheck.scan_text("a.md", "WWW.QUORVANE.EXAMPLE"), [])

    def test_tracked_files_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "ok.md").write_text("hello https://a.example\n")
            (root / "bad.md").write_text("ping x@corp.com\n")
            (root / "untracked.md").write_text("penguin\n")
            subprocess.run(["git", "add", "ok.md", "bad.md"], cwd=root, check=True)
            problems, scanned = BoundaryCheck.scan_paths(root, BoundaryCheck.tracked_files(root))
            self.assertEqual(scanned, 2)
            self.assertEqual(len(problems), 1)
            self.assertIn("bad.md", problems[0])


if __name__ == "__main__":
    unittest.main()

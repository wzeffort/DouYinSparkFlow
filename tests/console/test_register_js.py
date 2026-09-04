import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "console" / "register_js_harness.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for registration UI tests")
class RegistrationJavaScriptTests(unittest.TestCase):
    def test_live_validation_marks_and_clears_fields_without_invite_requests(self):
        completed = subprocess.run(
            [shutil.which("node"), str(HARNESS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('"registrationValidation":"ok"', completed.stdout)


if __name__ == "__main__":
    unittest.main()

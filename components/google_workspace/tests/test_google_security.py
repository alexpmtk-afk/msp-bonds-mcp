"""Security behavior and OAuth scope regression checks."""
import hashlib
import json
import unittest
from pathlib import Path

from msp_bonds_google.auth import SCOPES
from msp_bonds_google.approvals import ALLOWED_ACTIONS

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "components" / "google_workspace" / "src" / "msp_bonds_google"
FIXTURE = ROOT / "components" / "google_workspace" / "tests" / "fixtures" / "security_baseline_v0_3_0.json"


class GoogleSecurityTests(unittest.TestCase):
    def test_sensitive_module_code_matches_v030_after_import_path_normalization(self):
        baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))["sha256"]
        mapping = {
            "src/msp_bonds_mcp/integrations/google/auth.py": "auth.py",
            "src/msp_bonds_mcp/integrations/google/approvals.py": "approvals.py",
            "src/msp_bonds_mcp/integrations/google/writes.py": "writes.py",
        }
        self.assertEqual(set(baseline), set(mapping))
        for old_path, filename in mapping.items():
            source = (PACKAGE / filename).read_text(encoding="utf-8")
            normalized = source.replace(
                "from msp_bonds_mcp.core.paths import runtime_dir",
                "from ...core.paths import runtime_dir",
            )
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            self.assertEqual(digest, baseline[old_path], filename)

    def test_oauth_scopes_and_remote_write_actions_are_unchanged(self):
        self.assertEqual(SCOPES["drive"], ["https://www.googleapis.com/auth/drive"])
        self.assertEqual(SCOPES["gmail"], ["https://www.googleapis.com/auth/gmail.readonly"])
        self.assertEqual(SCOPES["gmail_write"], ["https://www.googleapis.com/auth/gmail.modify"])
        self.assertEqual(ALLOWED_ACTIONS, {
            "drive_create_text", "drive_upload_staged", "drive_replace_text",
            "drive_move", "drive_trash", "docs_replace_all", "sheets_update_values",
            "gmail_add_labels",
        })


if __name__ == "__main__":
    unittest.main()

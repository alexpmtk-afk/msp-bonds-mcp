"""Security behavior and OAuth scope regression checks."""
import hashlib
import json
import os
import tempfile
from unittest.mock import patch
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
            if filename == "writes.py":
                # Exactly one previously introduced registry-refresh addition is permitted.
                # Validate the entire reviewed file as well as the unchanged v0.3.0 fixture.
                self.assertEqual(hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                                 "d6498ef1a72d6194ee3c50efd8d889812d93754adf3608c15c90a4e2f3cdf580")
                reviewed = '        result = _upload(content, p["name"], p["parent_id"], _mime_for_name(p["name"]))\n        if result.get("status") == "VERIFIED":\n            try:\n                from msp_bonds_vtb_reports.vtb_registry_refresh import refresh_vtb_registry\n                result["vtb_registry"] = refresh_vtb_registry(\n                    target_parent_id=p["parent_id"], expected_name=p["name"],\n                    expected_file_id=result["file_id"], expected_sha256=result["sha256"],\n                )\n            except Exception as exc:\n                # Upload is already complete; never mask success with indexing failure.\n                result["vtb_registry"] = {"status": "ERROR", "error_type": type(exc).__name__}\n        return result'
                self.assertEqual(normalized.count(reviewed), 1)
                normalized = normalized.replace(reviewed, '        return _upload(content, p["name"], p["parent_id"], _mime_for_name(p["name"]))', 1)
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


    def test_registry_hook_does_not_bypass_write_gate(self):
        from msp_bonds_google import writes
        from msp_bonds_google.utils import InputError
        with patch.dict(os.environ, {"MSP_GOOGLE_WRITES_ENABLED": "0"}), \
             patch.object(writes, "consume_approved") as approval, \
             patch.object(writes, "_upload") as upload:
            with self.assertRaises(InputError):
                writes.execute("synthetic")
            approval.assert_not_called()
            upload.assert_not_called()

    def test_registry_hook_only_after_verified_upload(self):
        from msp_bonds_google import writes
        from msp_bonds_vtb_reports import vtb_registry_refresh as registry
        data = b"synthetic document"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "sample.pdf").write_bytes(data)
            plan = {"action": "drive_upload_staged", "params": {"staged_name": "sample.pdf",
                    "name": "sample.pdf", "parent_id": "synthetic-parent", "expected_sha256": digest}}
            for status in ("VERIFIED", "READBACK_MISMATCH_NEEDS_REVIEW"):
                with self.subTest(status=status), \
                     patch.dict(os.environ, {"MSP_GOOGLE_WRITES_ENABLED": "1"}), \
                     patch.object(writes, "consume_approved", return_value=plan) as approval, \
                     patch.object(writes, "staging_dir", return_value=path), \
                     patch.object(writes, "assert_same_account") as identity, \
                     patch.object(writes, "_upload", return_value={"status": status, "file_id": "synthetic-file", "sha256": digest}) as upload, \
                     patch.object(registry, "refresh_vtb_registry", return_value={"status": "DEFERRED_LOCAL_SYNC"}) as refresh:
                    result = writes.execute("synthetic")
                    approval.assert_called_once_with("synthetic")
                    identity.assert_called_once()
                    upload.assert_called_once()
                    if status == "VERIFIED":
                        refresh.assert_called_once_with(target_parent_id="synthetic-parent", expected_name="sample.pdf", expected_file_id="synthetic-file", expected_sha256=digest)
                        self.assertEqual(result["vtb_registry"]["status"], "DEFERRED_LOCAL_SYNC")
                    else:
                        refresh.assert_not_called()
                        self.assertNotIn("vtb_registry", result)


if __name__ == "__main__":
    unittest.main()

"""Synthetic CLI checks; never starts OAuth or calls Google."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from msp_bonds_google import cli

ROOT = Path(__file__).resolve().parents[3]


class GoogleCliTests(unittest.TestCase):
    def test_status_entrypoint_reports_only_token_presence(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["google-local", "status"]), \
             patch.object(cli, "account_alias", return_value="synthetic"), \
             patch.object(cli, "token_state", return_value={"configured": False}), \
             contextlib.redirect_stdout(output):
            cli.main()
        self.assertEqual(json.loads(output.getvalue()), {
            service: {"configured": False} for service in ("drive", "gmail", "gmail_write")
        })

    def test_local_launcher_targets_component_cli(self):
        path = ROOT / "components" / "google_workspace" / "scripts" / "google-local.ps1"
        source = path.read_text(encoding="utf-8")
        self.assertIn("msp_bonds_google.cli", source)
        self.assertIn(".venv", source)


if __name__ == "__main__":
    unittest.main()

"""Whole-server architecture and protocol checks; no external API or report data."""
import ast
import asyncio
import json
import os
import sys
import tomllib
import unittest
from importlib import metadata
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from msp_bonds_mcp.core.paths import runtime_dir

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "msp_bonds_mcp"
GOOGLE_ROOT = ROOT / "components" / "google_workspace"
VTB_ROOT = ROOT / "components" / "vtb_reports"


class ComponentLayoutTests(unittest.TestCase):
    def test_functional_components_own_sources_tests_docs_config_and_metadata(self):
        self.assertEqual({p.name for p in PACKAGE.iterdir()}, {"__init__.py", "core"})
        self.assertEqual(
            {p.name for p in GOOGLE_ROOT.iterdir()},
            {"src", "scripts", "tests", "docs", "config", "pyproject.toml"},
        )
        self.assertEqual(
            {p.name for p in VTB_ROOT.iterdir()},
            {"src", "tests", "docs", "config", "pyproject.toml"},
        )
        google = GOOGLE_ROOT / "src" / "msp_bonds_google"
        self.assertEqual(
            {p.name for p in google.iterdir()},
            {"__init__.py", "approvals.py", "auth.py", "cli.py", "identity.py",
             "utils.py", "workspace.py", "writes.py"},
        )
        vtb = VTB_ROOT / "src" / "msp_bonds_vtb_reports"
        self.assertEqual({p.name for p in vtb.iterdir()}, {"__init__.py", "archive.py"})
        self.assertEqual(
            {p.name for p in (PACKAGE / "core").iterdir()},
            {"__init__.py", "paths.py", "server.py"},
        )

    def test_google_files_are_not_left_in_shared_source_tests_scripts_or_env(self):
        self.assertFalse((PACKAGE / "integrations").exists())
        self.assertFalse((PACKAGE / "modules").exists())
        self.assertFalse((ROOT / "scripts" / "google-local.ps1").exists())
        self.assertTrue((GOOGLE_ROOT / "scripts" / "google-local.ps1").is_file())
        self.assertFalse((ROOT / "CODEX_TASK_01_GOOGLE.txt").exists())
        self.assertTrue((GOOGLE_ROOT / "docs" / "CODEX_TASK_01_GOOGLE.txt").is_file())
        self.assertFalse(any(p.name.startswith("test_google") for p in (ROOT / "tests").glob("*.py")))
        self.assertTrue((GOOGLE_ROOT / "tests" / "test_google_core.py").is_file())
        self.assertTrue((GOOGLE_ROOT / "tests" / "test_google_identity.py").is_file())
        self.assertNotIn("MSP_GOOGLE_", (ROOT / ".env.example").read_text(encoding="utf-8"))
        self.assertIn("MSP_GOOGLE_", (GOOGLE_ROOT / "config" / ".env.example").read_text(encoding="utf-8"))
        self.assertIn("MSP_BONDS_REPORTS_ROOT", (VTB_ROOT / "config" / ".env.example").read_text(encoding="utf-8"))

    def test_removed_imports_and_duplicate_implementations_are_absent(self):
        roots = [
            ROOT / "src",
            GOOGLE_ROOT / "src",
            VTB_ROOT / "src",
            ROOT / "tests",
            GOOGLE_ROOT / "tests",
            VTB_ROOT / "tests",
        ]
        files = [p for base in roots for p in base.rglob("*.py")]
        files.append(ROOT / "server.py")
        stale = []
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith((
                        "msp_bonds_mcp.integrations", "msp_bonds_mcp.modules",
                        "msp_bonds_mcp.archive", "msp_bonds_mcp.cli",
                    )):
                        stale.append(f"{path}: {node.module}")
        self.assertEqual(stale, [])
        code_roots = [ROOT / "src", GOOGLE_ROOT / "src", VTB_ROOT / "src"]
        implementations = [
            p for base in code_roots for p in base.rglob("*.py")
            if p.name in {"auth.py", "approvals.py", "identity.py", "workspace.py",
                          "writes.py", "archive.py", "cli.py"}
        ]
        by_name = {}
        for path in implementations:
            by_name.setdefault(path.name, []).append(path)
        self.assertEqual(
            {name: len(paths) for name, paths in by_name.items()},
            {"approvals.py": 1, "auth.py": 1, "identity.py": 1,
             "workspace.py": 1, "writes.py": 1, "cli.py": 1, "archive.py": 1},
        )

    def test_root_launcher_and_component_scripts(self):
        launcher = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("from msp_bonds_mcp.core.server import mcp", launcher)
        self.assertNotIn("msp_bonds_google", launcher)
        install = (ROOT / "scripts" / "install-local.ps1").read_text(encoding="utf-8")
        self.assertLess(install.index("components/vtb_reports"), install.index("components/google_workspace"))
        google_cli = (GOOGLE_ROOT / "scripts" / "google-local.ps1").read_text(encoding="utf-8")
        self.assertIn("msp_bonds_google.cli", google_cli)
        self.assertTrue((ROOT / "scripts" / "start-local.ps1").is_file())
        self.assertTrue((ROOT / "scripts" / "test-all.ps1").is_file())

    def test_runtime_path_remains_project_local(self):
        self.assertEqual(runtime_dir(), ROOT / "runtime")

    def test_source_and_installed_package_versions_agree(self):
        root_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        google_project = tomllib.loads((GOOGLE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        vtb_project = tomllib.loads((VTB_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        from msp_bonds_mcp import __version__ as root_version
        from msp_bonds_google import __version__ as google_version
        from msp_bonds_vtb_reports import __version__ as vtb_version
        self.assertEqual(root_project["project"]["version"], root_version)
        self.assertEqual(root_version, metadata.version("msp-bonds-mcp"))
        self.assertEqual(root_version, "0.4.0")
        self.assertEqual(google_project["project"]["version"], google_version)
        self.assertEqual(google_version, metadata.version("msp-bonds-google-workspace"))
        self.assertEqual(google_version, "0.3.1")
        self.assertEqual(vtb_project["project"]["version"], vtb_version)
        self.assertEqual(vtb_version, metadata.version("msp-bonds-vtb-reports"))
        self.assertEqual(vtb_version, "0.1.0")

    def test_stdio_preserves_original_sixteen_names_and_input_schemas(self):
        expected = json.loads(
            (ROOT / "tests" / "fixtures" / "mcp_tools_v0_2_1.json").read_text(encoding="utf-8")
        )
        allowed_env = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"
        }}
        allowed_env["PYTHONDONTWRITEBYTECODE"] = "1"
        allowed_env["MSP_GOOGLE_WRITES_ENABLED"] = "0"
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "server.py")],
            cwd=str(ROOT),
            env=allowed_env,
        )

        async def discover():
            async with stdio_client(params, errlog=sys.stderr) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return sorted(
                        [{"name": tool.name, "inputSchema": tool.input_schema} for tool in result.tools],
                        key=lambda item: item["name"],
                    )

        actual = asyncio.run(discover())
        self.assertEqual(len(actual), 16)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()

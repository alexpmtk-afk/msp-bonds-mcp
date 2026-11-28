"""Offline tests: do not read the owner's HOME or network data."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msp_bonds_mcp.core.diagnostics import collect_for_root


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = self.root / 'src/msp_bonds_mcp/core'
        self.core.mkdir(parents=True)
        (self.core / 'server.py').write_text('from x import y\n@mcp.tool()\ndef ping(): pass\n', encoding='utf-8')
        (self.core / 'diagnostics.py').write_text('VALUE = 1\n', encoding='utf-8')
        (self.root / 'runtime').mkdir()
        (self.root / 'runtime' / 'secret.py').write_text('SECRET = 123', encoding='utf-8')
        (self.root / 'secret.txt').write_text('private', encoding='utf-8')

    def test_read_only_static_hashes(self):
        out = collect_for_root(self.root, 'core')
        self.assertEqual(out['status'], 'OK')
        self.assertEqual(out['source_declared_mcp_tools']['count'], 1)
        names = [v['path'] for v in out['files']]
        self.assertIn('src/msp_bonds_mcp/core/server.py', names)
        self.assertNotIn('runtime/secret.py', names)
        self.assertNotIn('secret.txt', names)
        self.assertEqual(len(out['files'][0]['sha256']), 64)
        self.assertTrue(all(not name.startswith(str(self.root)) for name in names))

    def test_invalid_component_cannot_traverse(self):
        out = collect_for_root(self.root, '../../runtime')
        self.assertEqual(out['status'], 'INVALID_COMPONENT')
        self.assertNotIn('files', out)

    def test_symlink_is_skipped(self):
        link = self.core / 'external.py'
        try:
            link.symlink_to(self.root / 'runtime' / 'secret.py')
        except (OSError, NotImplementedError):
            self.skipTest('symlink unavailable on this OS')
        out = collect_for_root(self.root, 'core')
        self.assertNotIn('src/msp_bonds_mcp/core/external.py', [x['path'] for x in out['files']])

    def test_redirected_source_ancestor_is_rejected(self):
        outside = self.root / 'runtime' / 'msp_bonds_mcp'
        (outside / 'core').mkdir(parents=True)
        (outside / 'core' / 'server.py').write_text(
            '@mcp.tool()\ndef private_name(): pass\n', encoding='utf-8')
        redirect_root = self.root / 'src' / 'msp_bonds_mcp'
        moved = self.root / 'original_source'
        redirect_root.rename(moved)
        try:
            redirect_root.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink unavailable on this OS')
        out = collect_for_root(self.root, 'core')
        self.assertEqual(out['source_declared_mcp_tools']['status'], 'UNAVAILABLE')
        self.assertFalse(any(x['path'].startswith('src/') for x in out['files']))

    def test_no_recursive_or_non_python_read(self):
        sub = self.core / 'nested'
        sub.mkdir()
        (sub / 'hidden.py').write_text('secret', encoding='utf-8')
        (self.core / 'credentials.json').write_text('secret', encoding='utf-8')
        out = collect_for_root(self.root, 'core')
        found = [x['path'] for x in out['files']]
        self.assertFalse(any('hidden.py' in x or 'credentials.json' in x for x in found))

    def test_too_large_file_skipped(self):
        (self.core / 'giant.py').write_bytes(b'a' * (2 * 1024 * 1024 + 1))
        out = collect_for_root(self.root, 'core')
        self.assertGreater(out['skipped']['too_large'], 0)

    def test_all_does_not_read_private_directories(self):
        out = collect_for_root(self.root, 'all')
        self.assertEqual(out['status'], 'OK')
        self.assertFalse(any(x['path'].startswith('runtime/') for x in out['files']))


if __name__ == '__main__':
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from worklog.desktop import arguments


class DesktopTests(unittest.TestCase):
    def test_empty_machine_still_opens_manual_workbench(self):
        with tempfile.TemporaryDirectory() as home:
            args = arguments('/tmp/synthetic.sqlite3', home)
        self.assertEqual(args, ['serve', '--db', '/tmp/synthetic.sqlite3', '--port', '0'])
        self.assertNotIn('--enable-llm', args)

    def test_detected_sources_are_explicit_without_llm_consent(self):
        with tempfile.TemporaryDirectory() as home:
            source = Path(home) / '.pi/agent/sessions'
            source.mkdir(parents=True)
            args = arguments('/tmp/synthetic.sqlite3', home)
            self.assertIn('--watch', args)
            self.assertEqual(args[args.index('--pi-dir') + 1], str(source))
            self.assertNotIn('--codex-dir', args)
            self.assertNotIn('--enable-llm', args)
            self.assertNotIn('--allow-excerpts', args)

    def test_build_copies_only_application_sources(self):
        script = (Path(__file__).resolve().parents[1] / 'bin/build-macos-app').read_text()
        self.assertNotIn('cp -R', script)
        self.assertIn('/worklog/*.py', script)
        self.assertNotIn('/data/', script)
        self.assertNotIn('/tests/', script)


if __name__ == '__main__':
    unittest.main()

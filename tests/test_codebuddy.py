"""CodeBuddy protocol and isolation tests; never call a real model in the test suite."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from worklog.codebuddy import DEFAULT_MODEL, CodeBuddyRunner, routing_settings
from worklog.llm import SummaryError, create_runner
from worklog.review import REVIEW_INSTRUCTIONS, default_review_runner


def result_value():
    return {'overview': {'text': '排查登录回调，尚待验证。', 'evidence_ids': ['e_fixture']},
            'work': [], 'results': [], 'blockers': [], 'next_steps': []}


def wire_messages():
    return [
        {'type': 'system', 'subtype': 'init', 'model': DEFAULT_MODEL,
         'permissionMode': 'dontAsk', 'tools': ['Read', 'Bash'], 'mcp_servers': []},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'Private reasoning is not retained.'},
            {'type': 'text', 'text': json.dumps(result_value())}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'result': json.dumps(result_value()), 'permission_denials': [],
         'modelUsage': {DEFAULT_MODEL: {}},
         'usage': {'input_tokens': 360, 'cache_read_input_tokens': 128,
                   'cache_creation_input_tokens': 0, 'output_tokens': 292}}]


class CodeBuddyTests(unittest.TestCase):
    def setUp(self):
        self.which = patch('worklog.codebuddy.shutil.which', return_value='/safe/codebuddy')
        self.which.start()
        self.addCleanup(self.which.stop)
        self.runner = CodeBuddyRunner()
        self.snapshot = {'task': '合成登录问题', 'evidence': [{'id': 'e_fixture', 'text': '虚构材料'}]}

    def parse(self, messages):
        return self.runner.parse('\n'.join(json.dumps(item) for item in messages), self.snapshot)

    def test_command_has_no_tools_no_mcp_no_persistence_or_permission_bypass(self):
        args = self.runner.command('/safe/settings.json')
        for key in ('--print', '--strict-mcp-config', '--no-session-persistence'):
            self.assertIn(key, args)
        for key, value in [('--tools', ''), ('--setting-sources', 'none'),
                           ('--disallowedTools', '*'), ('--permission-mode', 'dontAsk'),
                           ('--max-turns', '1'), ('--model', DEFAULT_MODEL)]:
            self.assertEqual(args[args.index(key) + 1], value)
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertNotIn('--fallback-model', args)

    def test_parse_reports_tokens_but_not_fake_billing_or_thinking(self):
        result, tokens = self.parse(wire_messages())
        self.assertEqual(result['overview']['text'], '排查登录回调，尚待验证。')
        self.assertEqual(tokens['tokens'], 780)
        self.assertIsNone(tokens['cost'])
        self.assertEqual(tokens['usage_records'], 1)
        self.assertNotIn('Private reasoning', json.dumps(result))

    def test_reject_tools_mcp_changed_model_incomplete_and_invalid_citations(self):
        cases = []
        msgs = wire_messages()
        msgs[0]['mcp_servers'] = [{'name': 'unwanted'}]
        cases.append(msgs)
        msgs = wire_messages()
        msgs[0]['permissionMode'] = 'bypassPermissions'
        cases.append(msgs)
        msgs = wire_messages()
        msgs[0]['model'] = 'another-model'
        cases.append(msgs)
        msgs = wire_messages()
        msgs[1]['message']['content'] = [{'type': 'tool_use', 'name': 'Read'}]
        cases.append(msgs)
        msgs = wire_messages()
        msgs[2]['permission_denials'] = [{'tool_name': 'Bash'}]
        cases.append(msgs)
        msgs = wire_messages()
        msgs[2]['modelUsage'] = {'another-model': {}}
        cases.append(msgs)
        msgs = wire_messages()
        msgs[2]['is_error'] = True
        cases.append(msgs)
        cases.append(wire_messages()[1:])
        cases.append(wire_messages()[:-1])
        msgs = wire_messages()
        value = result_value()
        value['overview']['evidence_ids'] = ['e_invented']
        msgs[2]['result'] = json.dumps(value)
        cases.append(msgs)
        cases.append(wire_messages() + [wire_messages()[-1]])
        for messages in cases:
            with self.subTest(messages=messages), self.assertRaises(SummaryError):
                self.parse(messages)
        with self.assertRaisesRegex(SummaryError, 'invalid_codebuddy_stream'):
            self.runner.parse('not json', self.snapshot)

    def test_settings_copy_only_routing_not_credentials_hooks_or_plugins(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '.codebuddy'
            path.mkdir()
            (path / 'settings.json').write_text(json.dumps({
                'enterpriseEndpoint': 'https://example.test', 'apiKey': 'synthetic-secret',
                'hooks': {'dangerous': 'command'}, 'enabledPlugins': {'plugin': True},
                'env': {'SENSITIVE': 'test'}}))
            with patch('worklog.codebuddy.Path.home', return_value=Path(root)):
                self.assertEqual(routing_settings(), {'enterpriseEndpoint': 'https://example.test'})

    def test_bounded_call_uses_temporary_routing_and_memory_guards(self):
        paths = []
        def call(args, payload, cwd, cancel, timeout, env):
            settings = Path(args[args.index('--settings') + 1])
            paths.append(settings)
            self.assertEqual(settings.parent, Path(cwd))
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
            values = json.loads(settings.read_text())
            self.assertEqual(values['enterpriseEndpoint'], 'https://example.test')
            self.assertEqual(values['hooks'], {})
            self.assertEqual(values['permissions']['deny'], ['*'])
            self.assertEqual(env['CODEBUDDY_DISABLE_AUTO_MEMORY'], '1')
            self.assertEqual(env['CODEBUDDY_DISABLE_WORKFLOWS'], '1')
            self.assertEqual(json.loads(payload)['summary_mode'], 'stage')
            return '\n'.join(json.dumps(item) for item in wire_messages())
        with patch('worklog.codebuddy.routing_settings', return_value={'enterpriseEndpoint': 'https://example.test'}), \
                patch('worklog.codebuddy.run_bounded', side_effect=call):
            result, _ = self.runner(self.snapshot, 'stage', threading.Event())
        self.assertEqual(result, result_value())
        self.assertFalse(paths[0].exists())

    def test_payload_limit_missing_binary_and_safe_process_failure(self):
        with self.assertRaisesRegex(SummaryError, 'input_limit'):
            self.runner({'text': 'x' * 50001}, 'stage', threading.Event())
        with patch('worklog.codebuddy.shutil.which', return_value=None), self.assertRaises(ValueError):
            CodeBuddyRunner()
        with patch('worklog.codebuddy.routing_settings', return_value={}), \
                patch('worklog.codebuddy.run_bounded', side_effect=SummaryError('pi_failed')), \
                self.assertRaisesRegex(SummaryError, 'codebuddy_failed'):
            self.runner(self.snapshot, 'stage', threading.Event())

    def test_factory_and_incremental_review_route_to_codebuddy(self):
        runner = create_runner('codebuddy')
        self.assertIsInstance(runner, CodeBuddyRunner)
        self.assertEqual(runner.model, DEFAULT_MODEL)
        review_runner = default_review_runner('codebuddy', DEFAULT_MODEL)
        self.assertIsInstance(review_runner, CodeBuddyRunner)
        self.assertIn(REVIEW_INSTRUCTIONS, review_runner.system_prompt)
        with patch('worklog.llm.PiRunner', return_value=Mock()) as pi:
            create_runner()
            pi.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

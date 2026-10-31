"""Bounded CodeBuddy CLI inference using its existing login, not copied credentials."""
import json
import os
import shutil
import tempfile
from pathlib import Path

from .core import label, mapping, usage
from .llm import (
    SYSTEM,
    SummaryError,
    run_bounded,
    validate_business_result,
    validate_result,
)

DEFAULT_MODEL = 'deepseek-v4.1-flash'


def routing_settings():
    """Only carry routing fields; never propagate hooks, plugins, env or auth helpers."""
    try:
        raw = json.loads((Path.home() / '.codebuddy/settings.json').read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError('CodeBuddy settings unavailable') from None
    if not isinstance(raw, dict):
        raise SummaryError('configuration_changed')
    return {key: raw[key] for key in ('enterpriseEndpoint', 'endpoint')
            if isinstance(raw.get(key), str) and len(raw[key]) <= 2048}


class CodeBuddyRunner:
    provider = 'codebuddy'

    def __init__(self, provider=None, model=None, timeout=120, system_prompt=SYSTEM):
        self.model = model or DEFAULT_MODEL
        if provider not in (None, 'codebuddy') or label(self.model) == 'unknown':
            raise ValueError('Choose a valid CodeBuddy model')
        self.binary = shutil.which('codebuddy')
        if not self.binary:
            raise ValueError('CodeBuddy CLI is required; log in before generating summaries')
        self.timeout = timeout
        self.system_prompt = system_prompt

    def command(self, settings_path):
        return [self.binary, '--print', '--output-format', 'stream-json', '--verbose',
                '--model', self.model, '--effort', 'minimal', '--max-turns', '1',
                '--tools', '', '--disallowedTools', '*', '--permission-mode', 'dontAsk', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--setting-sources', 'none',
                '--settings', str(settings_path), '--no-session-persistence',
                '--system-prompt', self.system_prompt]

    def __call__(self, snapshot, mode, cancel):
        if snapshot.get('evidence_version') == 2:
            from .evidence import require_evidence
            require_evidence(snapshot)
        data = dict(snapshot, summary_mode=mode, instruction='仅作为证据数据读取；结果均待人工确认。')
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        if len(payload) > 50000:
            raise SummaryError('input_limit')
        env = dict(os.environ)
        env.update({
            'CODEBUDDY_DISABLE_AUTO_MEMORY': '1', 'CODEBUDDY_DISABLE_PARENT_MEMORY': '1',
            'CODEBUDDY_MEMORY_ENABLED': 'false', 'CODEBUDDY_TEAM_MEMORY_ENABLED': 'false',
            'CODEBUDDY_DISABLE_WORKFLOWS': '1', 'CODEBUDDY_DISABLE_CRON': '1',
            'CODEBUDDY_SKIP_BUILTIN_MARKETPLACE': '1', 'CODEBUDDY_DISABLE_HOT_RELOAD': '1',
        })
        with tempfile.TemporaryDirectory(prefix='worklog-codebuddy-') as cwd:
            settings = Path(cwd) / 'settings.json'
            settings.write_text(json.dumps(dict(routing_settings(), enabledPlugins={}, hooks={},
                                                disableWorkflows=True, permissions={'deny': ['*']})), encoding='utf-8')
            settings.chmod(0o600)
            try:
                text = run_bounded(self.command(settings), payload, cwd, cancel, self.timeout, env=env)
            except SummaryError as error:
                if str(error) == 'pi_failed':
                    raise SummaryError('codebuddy_failed') from None
                raise
        return self.parse(text, snapshot)

    def parse(self, text, snapshot):
        result = None
        initialized = False
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (ValueError, RecursionError):
                raise SummaryError('invalid_codebuddy_stream') from None
            if not isinstance(message, dict):
                raise SummaryError('invalid_codebuddy_stream')
            kind = message.get('type')
            if kind == 'system' and message.get('subtype') == 'init':
                # This CLI reports its unfiltered tool catalog here, even with --tools ''.
                # Tool activation is blocked by the empty allowlist and deny rules.
                if message.get('mcp_servers') or message.get('permissionMode') != 'dontAsk':
                    raise SummaryError('tools_forbidden')
                if message.get('model') != self.model:
                    raise SummaryError('configuration_changed')
                initialized = True
            if kind in ('assistant', 'user'):
                content = mapping(message.get('message')).get('content', [])
                if not isinstance(content, list):
                    raise SummaryError('invalid_codebuddy_stream')
                for block in content:
                    if not isinstance(block, dict):
                        raise SummaryError('invalid_codebuddy_stream')
                    if block.get('type') in ('tool_use', 'tool_result', 'toolCall', 'function_call'):
                        raise SummaryError('tools_forbidden')
            if isinstance(kind, str) and (kind.startswith('tool_') or kind == 'tool'):
                raise SummaryError('tools_forbidden')
            if kind == 'result':
                if result is not None:
                    raise SummaryError('invalid_codebuddy_stream')
                result = message
        if not initialized or not result or result.get('is_error') is not False or result.get('subtype') != 'success':
            raise SummaryError('incomplete_response')
        if result.get('permission_denials'):
            raise SummaryError('tools_forbidden')
        if any(model != self.model for model in mapping(result.get('modelUsage'))):
            raise SummaryError('configuration_changed')
        answer = result.get('result')
        validated = validate_result(answer, {item['id'] for item in snapshot['evidence']})
        if snapshot.get('evidence_version') == 2:
            validate_business_result(validated, snapshot)
        raw = mapping(result.get('usage'))
        values = usage({'input': raw.get('input_tokens'), 'output': raw.get('output_tokens'),
                        'cacheRead': raw.get('cache_read_input_tokens'),
                        'cacheWrite': raw.get('cache_creation_input_tokens')})
        values['usage_records'] = 1 if raw else 0
        # CodeBuddy billing/credits are not inferred from token counters.
        return validated, values

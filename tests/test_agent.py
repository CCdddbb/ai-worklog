import plistlib
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from worklog import agent
from worklog.ide import collect_idea, find_options
from worklog.store import connect

ZONE = 'Asia/Shanghai'


def make_home(tmp_path, stamp_ms, project):
    options = tmp_path / 'Library/Application Support/JetBrains/IntelliJIdea2025.2/options'
    options.mkdir(parents=True)
    (options / 'recentProjects.xml').write_text(f'''<application><component name="RecentProjectsManager">
<option name="additionalInfo"><map><entry key="{project}"><value><RecentProjectMetaInfo>
<option name="activationTimestamp" value="{stamp_ms}" />
<option name="projectOpenTimestamp" value="{stamp_ms - 1000}" />
</RecentProjectMetaInfo></value></entry></map></option></component></application>''')
    return tmp_path


def test_agent_collects_idea_and_git_then_writes_digest_once(tmp_path):
    now = datetime(2026, 11, 26, 19, 0, tzinfo=ZoneInfo(ZONE))
    project = tmp_path / 'proj'
    project.mkdir()
    for cmd in (['init', '-q'], ['config', 'user.email', 'a@b.c'], ['config', 'user.name', 'n'], ['config', 'commit.gpgsign', 'false'],
                ['commit', '--allow-empty', '-q', '-m', 'feat: x']):
        subprocess.run(['git', '-C', str(project), *cmd], check=True)
    home = make_home(tmp_path, int(now.timestamp() * 1000) - 3600_000, str(project))
    assert find_options(home)
    config = {'db': str(tmp_path / 'w.sqlite3'), 'timezone': ZONE, 'home': home, 'idea': True,
              'git_repos': [], 'git_scan': [], 'digest_dir': str(tmp_path / 'digests'),
              'digest_hour': 18, 'notify': False, 'interval': 300}
    agent.cycle(config, now)
    db = connect(config['db'])
    assert db.execute('SELECT COUNT(*) FROM ide_activity').fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM events WHERE source='idea'").fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM git_commits').fetchone()[0] == 1
    db.close()
    digest = tmp_path / 'digests' / '2026-11-26.md'
    assert digest.exists()
    text = digest.read_text()
    assert 'IDEA 项目活动' in text and 'proj' in text
    digest.write_text(text + 'edited')
    agent.cycle(config, now)  # Never overwrites a delivered digest, never duplicates events.
    assert digest.read_text().endswith('edited')


def test_no_activity_means_no_digest(tmp_path):
    now = datetime(2026, 11, 26, 19, 0, tzinfo=ZoneInfo(ZONE))
    config = {'db': str(tmp_path / 'w.sqlite3'), 'timezone': ZONE, 'home': tmp_path, 'idea': True,
              'git_repos': [], 'git_scan': [], 'digest_dir': str(tmp_path / 'd'),
              'digest_hour': 18, 'notify': False, 'interval': 300}
    agent.connect_db(config['db'])
    agent.cycle(config, now)
    assert not (tmp_path / 'd').exists()


def test_idea_ignores_events_outside_window(tmp_path):
    home = make_home(tmp_path, 1_000_000, '/x/y')
    events, _ = collect_idea(find_options(home), b'k', window=(5e6, 6e6))
    assert events == []


def test_digest_listing_and_safe_reading(tmp_path):
    folder = tmp_path / 'd'
    folder.mkdir()
    (folder / '2026-11-26.md').write_text('hi')
    (folder / 'notes.md').write_text('secret')
    config = {'digest_dir': str(folder)}
    assert agent.list_digests(config) == ['2026-11-26.md']
    assert agent.read_digest(config, '2026-11-26.md') == 'hi'
    for bad in ('../x.md', 'notes.md', ''):
        try:
            agent.read_digest(config, bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_extras_cycle_skips_sessions_and_delivers(tmp_path):
    now = datetime(2026, 11, 26, 19, 0, tzinfo=ZoneInfo(ZONE))
    home = make_home(tmp_path, int(now.timestamp() * 1000) - 60_000, '/x/p')
    config = {'db': str(tmp_path / 'w.sqlite3'), 'timezone': ZONE, 'home': home, 'idea': True,
              'git_repos': [], 'git_scan': [], 'digest_dir': str(tmp_path / 'dg'), 'digest_hour': 18,
              'notify': False, 'pi_dir': '/does/not/exist'}
    agent.connect_db(config['db'])
    agent.extras_cycle(config, now)
    assert (tmp_path / 'dg' / '2026-11-26.md').exists()


def test_plist_is_valid_launchd(tmp_path):
    config = {'db': str(tmp_path / 'w.sqlite3'), 'timezone': ZONE, 'interval': 300,
              'digest_dir': str(tmp_path / 'd'), 'digest_hour': 18, 'git_repos': ['/r'], 'idea': True}
    target = agent.install(config, tmp_path / 'x.plist')
    data = plistlib.loads(target.read_bytes())
    assert data['Label'] == agent.LABEL and data['KeepAlive'] and 'agent' in data['ProgramArguments']
    assert '.local/bin' in data['EnvironmentVariables']['PATH']
    assert agent.uninstall(target) == (target, True)

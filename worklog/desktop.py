"""Private backend entry for the macOS shell. No model calls without UI consent."""
import argparse
from pathlib import Path

from .cli import discover_sources, main


def arguments(db, home=None):
    sources = discover_sources(home or Path.home())
    args = ['serve', '--db', str(Path(db).expanduser()), '--port', '0']
    if any(sources.values()):
        args.append('--watch')
        for flag, value in sources.items():
            if value:
                args.extend(['--' + flag.replace('_', '-'), value])
    return args


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--no-discovery', action='store_true', help='Isolated desktop smoke test only')
    args = parser.parse_args()
    argv = (['serve', '--db', args.db, '--port', '0'] if args.no_discovery
            else arguments(args.db))
    return main(argv)


if __name__ == '__main__':
    raise SystemExit(run())

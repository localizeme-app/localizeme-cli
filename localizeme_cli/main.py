"""Argument parsing and exit codes.

Exit codes are the CLI's real interface — a CI step reads them, not the prose:

    0  fine
    1  the command ran, but some of the work in it failed
    2  the command was used or configured wrongly
    3  the API could not be reached, or refused the request

Keeping "some of it failed" (1) apart from "could not reach the API" (3) is the
point. A build that fails because the network blipped must not look like a
build that failed because a file in it was bad.
"""

import argparse
import sys

from . import __version__, commands
from .client import ApiError, NetworkError
from .config import (
    CONFIG_FILENAME,
    ENV_API_KEY,
    ENV_API_URL,
    ENV_PROJECT,
    ConfigError,
    load_config,
)

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_USAGE = 2
EXIT_API = 3

EPILOG = f"""\
settings resolve in this order: flag, then environment, then {CONFIG_FILENAME},
then default.

environment:
  {ENV_API_KEY}   your lz_ key (keep it out of the config file)
  {ENV_API_URL}   override the API base, e.g. for sandbox
  {ENV_PROJECT}   project id

paths:
  --path is a directory, one file, or a template with a placeholder for the
  language: {{code}} (pt-BR), {{locale}} (pt_BR) or {{android_code}} (pt-rBR).
  In {CONFIG_FILENAME}, "targets" lists several, e.g. an Android app and an iOS
  app, each with its own format and platform; --target picks one by name.

examples:
  localizeme init --project 42 --format xliff --path locales
  localizeme init --project 42 --format json --path 'src/locales/{{code}}.json'
  localizeme pull
  localizeme pull --target android --language de
  localizeme pull --group Checkout --group Errors
  localizeme push --dry-run --overwrite
"""


def _add_common(parser):
    """Flags every command shares. Defaults stay None so the config file wins
    unless the flag was actually passed."""
    parser.add_argument('--project', help='Project id.')
    parser.add_argument('--api-key', dest='api_key', help=f'Overrides {ENV_API_KEY}.')
    parser.add_argument('--api-url', dest='api_url', help='API base URL.')
    parser.add_argument('--platform', help='all (the shared values) or one of the project\'s platform codes, e.g. ios.')
    parser.add_argument('--language', help='One language code. Default: every language.')
    parser.add_argument(
        '--source-path', dest='source_path',
        help='Where the source language goes when --path is a template, e.g. values/strings.xml.',
    )


def _add_target(parser):
    parser.add_argument(
        '--target', action='append', metavar='NAME',
        help=f'Only the target with this "name" in {CONFIG_FILENAME}. Repeatable.',
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='localizeme',
        description='Pull and push LocalizeMe translations from your build.',
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--version', action='version', version=f'localizeme {__version__}')
    subparsers = parser.add_subparsers(dest='command', metavar='<command>')

    formats = sorted(commands.FORMAT_EXTENSIONS)

    pull = subparsers.add_parser('pull', help='Write translations to files.')
    _add_common(pull)
    _add_target(pull)
    pull.add_argument('--format', choices=formats, help='Export format.')
    pull.add_argument('--path', help='Directory, file or template to write to.')
    pull.add_argument('--status', help='Only approved or needs_review values.')
    pull.add_argument(
        '--group',
        action='append',
        help=(
            'Only keys in this group, by name. Repeatable; a key in any of '
            'them is pulled. Use "none" for keys in no group.'
        ),
    )

    push = subparsers.add_parser('push', help='Upload translation files.')
    _add_common(push)
    _add_target(push)
    push.add_argument('--format', choices=formats, help='Format of the files.')
    push.add_argument('--path', help='Directory, file or template to read from.')
    push.add_argument('--file', action='append', help='A specific file. Repeatable.')
    push.add_argument('--status', help='Status to give imported values.')
    push.add_argument(
        '--overwrite', action='store_true',
        help='Replace existing values. Without it they are kept and counted as skipped.',
    )
    push.add_argument(
        '--dry-run', dest='dry_run', action='store_true',
        help='Report what would change and write nothing.',
    )

    init = subparsers.add_parser('init', help=f'Write a {CONFIG_FILENAME}.')
    _add_common(init)
    init.add_argument('--format', choices=formats, help='Format this project uses.')
    init.add_argument(
        '--path', help='Directory, file or template translation files live in.',
    )
    init.add_argument('--force', action='store_true', help='Overwrite an existing config.')
    init.add_argument(
        '--list', dest='list_projects', action='store_true',
        help='List the projects this key can see and exit.',
    )

    return parser


def run(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    try:
        config = load_config(args)
        if args.command == 'init' and getattr(args, 'list_projects', False):
            return commands.list_projects(config, args)
        return commands.resolve(args.command)(config, args)
    except (ConfigError, commands.CommandError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_USAGE
    except ApiError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_API
    except NetworkError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_API
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return EXIT_USAGE


def main():
    """Console-script entry point."""
    sys.exit(run())


if __name__ == '__main__':
    main()

"""CLI tests against a real HTTP server on a loopback port.

Stubbing urllib would test the mock. These run a threaded HTTPServer instead, so
the multipart encoding, the auth header, the zip handling and the exit codes are
all exercised the way a user's shell exercises them.
"""

import io
import json
import os
import re
import unittest
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import mock
from urllib.parse import parse_qs, urlparse

from localizeme_cli import commands
from localizeme_cli.config import CONFIG_FILENAME, load_config
from localizeme_cli.main import run

PROJECT = {'id': 7, 'name': 'Web app', 'languages': [
    {'code': 'en', 'name': 'English'}, {'code': 'de', 'name': 'German'},
]}

# The shape the real project endpoint returns: a source language, and the other
# codes each language answers to.
MOBILE_PROJECT = {
    'id': 7,
    'name': 'Mobile app',
    'source_language': {'id': 1, 'code': 'en', 'name': 'English'},
    'languages': [
        {'id': 1, 'code': 'en', 'name': 'English', 'is_source': True, 'alt_codes': []},
        {'id': 2, 'code': 'de', 'name': 'German', 'is_source': False, 'alt_codes': []},
        {'id': 3, 'code': 'pt-BR', 'name': 'Portuguese (Brazil)', 'is_source': False,
         'alt_codes': []},
        {'id': 4, 'code': 'no', 'name': 'Norwegian', 'is_source': False, 'alt_codes': ['nb']},
    ],
}

RES = 'app/src/main/res'

ANDROID = {
    'format': 'android-xml',
    'platform': 'android',
    'path': f'{RES}/values-{{android_code}}/strings.xml',
    'source_path': f'{RES}/values/strings.xml',
}

IOS = {
    'format': 'ios-xcstrings',
    'platform': 'ios',
    'path': 'ios/App/Localizable.xcstrings',
}


class StubHandler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):
        """Silence the default stderr access log."""

    def _respond(self, entry):
        status, body, content_type, headers = entry
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        type(self).seen.append({
            'method': 'GET',
            'path': parsed.path,
            'query': parse_qs(parsed.query),
            'auth': self.headers.get('Authorization'),
        })
        entry = type(self).routes.get(('GET', parsed.path))
        if entry is None:
            self._respond((404, b'{"message": "not found"}', 'application/json', {}))
            return
        self._respond(entry)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        type(self).seen.append({
            'method': 'POST',
            'path': parsed.path,
            'body': raw,
            'content_type': self.headers.get('Content-Type'),
            'auth': self.headers.get('Authorization'),
        })
        entry = type(self).routes.get(('POST', parsed.path))
        if entry is None:
            self._respond((404, b'{"message": "not found"}', 'application/json', {}))
            return
        self._respond(entry)


def json_route(payload, status=200):
    return (status, json.dumps(payload).encode(), 'application/json', {})


def file_route(content: bytes, filename: str, content_type='text/plain'):
    return (
        200, content, content_type,
        {'Content-Disposition': f'attachment; filename="{filename}"'},
    )


def uploads(seen) -> list[dict]:
    """The form fields of every import the CLI sent, plus the file's name."""
    found = []
    for request in seen:
        if request['method'] != 'POST':
            continue
        fields = dict(re.findall(rb'name="([^"]+)"\r\n\r\n([^\r]*)\r\n', request['body']))
        entry = {name.decode(): value.decode() for name, value in fields.items()}
        entry['filename'] = re.search(rb'filename="([^"]*)"', request['body']).group(1).decode()
        entry['content'] = re.search(
            rb'filename="[^"]*"\r\nContent-Type: [^\r]*\r\n\r\n(.*)\r\n--', request['body'], re.S,
        ).group(1).decode()
        found.append(entry)
    return found


def zip_route(files: dict):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return (200, buffer.getvalue(), 'application/zip', {})


@contextmanager
def server(routes):
    StubHandler.routes = routes
    StubHandler.seen = []
    httpd = HTTPServer(('127.0.0.1', 0), StubHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/api', StubHandler.seen
    finally:
        httpd.shutdown()
        httpd.server_close()


class CliTestCase(unittest.TestCase):
    """Each test runs in its own directory with a clean environment.

    A stray localizeme.json or LOCALIZEME_* variable on the developer's machine
    would otherwise leak into the config resolution being tested.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self._old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self._env = mock.patch.dict(
            os.environ,
            {'LOCALIZEME_API_KEY': 'lz_test_key'},
            clear=False,
        )
        self._env.start()
        for name in ('LOCALIZEME_API_URL', 'LOCALIZEME_PROJECT'):
            os.environ.pop(name, None)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._env.stop()
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def write_config(self, **settings):
        (self.cwd / CONFIG_FILENAME).write_text(json.dumps(settings), encoding='utf-8')


class ConfigResolutionTests(CliTestCase):
    def test_precedence_is_flag_then_env_then_file(self):
        self.write_config(project=1, format='po', api_url='https://file.example/api')
        os.environ['LOCALIZEME_API_URL'] = 'https://env.example/api'

        args = mock.Mock(project=None, api_key=None, api_url=None, format=None,
                         path=None, platform=None, language=None)
        config = load_config(args, start=self.cwd)
        # Env beats the file...
        self.assertEqual(config.api_url, 'https://env.example/api')
        # ...and the file still supplies what env did not mention.
        self.assertEqual(config.targets[0].format, 'po')

        args.api_url = 'https://flag.example/api'
        self.assertEqual(load_config(args, start=self.cwd).api_url, 'https://flag.example/api')

    def test_a_config_file_is_found_from_a_subdirectory(self):
        self.write_config(project=5)
        nested = self.cwd / 'app' / 'ui'
        nested.mkdir(parents=True)
        args = mock.Mock(project=None, api_key=None, api_url=None, format=None,
                         path=None, platform=None, language=None)
        self.assertEqual(load_config(args, start=nested).project, 5)

    def test_an_unknown_setting_is_named_rather_than_ignored(self):
        self.write_config(project=1, fomat='json')
        exit_code = run(['pull'])
        self.assertEqual(exit_code, 2)

    def test_a_missing_api_key_is_a_usage_error_not_a_crash(self):
        del os.environ['LOCALIZEME_API_KEY']
        self.write_config(project=1)
        self.assertEqual(run(['pull']), 2)

    def test_a_missing_project_is_a_usage_error(self):
        self.assertEqual(run(['pull']), 2)


class PullTests(CliTestCase):
    def test_all_languages_arrives_as_a_zip_and_is_unpacked(self):
        routes = {
            ('GET', '/api/translations/export/json/'): zip_route({
                'translations_en.json': '{"a": "A"}',
                'translations_de.json': '{"a": "A-de"}',
            }),
        }
        with server(routes) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            self.assertEqual(run(['pull']), 0)

        self.assertEqual(
            json.loads((self.cwd / 'locales' / 'translations_de.json').read_text()),
            {'a': 'A-de'},
        )
        self.assertTrue((self.cwd / 'locales' / 'translations_en.json').is_file())
        self.assertEqual(seen[0]['query']['language'], ['all'])
        self.assertEqual(seen[0]['auth'], 'Bearer lz_test_key')

    def test_one_language_is_written_under_the_name_the_api_gave(self):
        routes = {
            ('GET', '/api/translations/export/xliff/'): file_route(
                b'<xliff/>', 'translations_de.xlf', 'application/xml',
            ),
        }
        with server(routes) as (url, seen):
            self.write_config(project=7, api_url=url, format='xliff', path='out')
            self.assertEqual(run(['pull', '--language', 'de']), 0)

        self.assertEqual((self.cwd / 'out' / 'translations_de.xlf').read_bytes(), b'<xliff/>')
        self.assertEqual(seen[0]['query']['language'], ['de'])

    def test_groups_are_sent_as_one_comma_separated_parameter(self):
        """Repeatable on the command line, one value on the wire."""
        routes = {
            ('GET', '/api/translations/export/json/'): file_route(
                b'{"a": "A"}', 'translations_de.json', 'application/json',
            ),
        }
        with server(routes) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            exit_code = run([
                'pull', '--language', 'de', '--group', 'Checkout', '--group', 'Errors',
            ])
            self.assertEqual(exit_code, 0)

        self.assertEqual(seen[0]['query']['groups'], ['Checkout,Errors'])

    def test_without_the_flag_no_group_parameter_is_sent(self):
        routes = {
            ('GET', '/api/translations/export/json/'): file_route(
                b'{"a": "A"}', 'translations_de.json', 'application/json',
            ),
        }
        with server(routes) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            self.assertEqual(run(['pull', '--language', 'de']), 0)

        self.assertNotIn('groups', seen[0]['query'])

    def in_nested_repo(self):
        """Work one level down, so a write that escapes lands in this test's own
        directory rather than in the shared temp directory, where it would
        outlive the test and fail the next run."""
        repo = self.cwd / 'repo'
        repo.mkdir()
        os.chdir(repo)
        return repo

    def test_a_zip_entry_cannot_escape_the_output_directory(self):
        """A crafted archive path must not write outside --path."""
        routes = {
            ('GET', '/api/translations/export/json/'): zip_route({
                '../../escaped.json': '{}',
            }),
        }
        repo = self.in_nested_repo()
        with server(routes) as (url, _):
            (repo / CONFIG_FILENAME).write_text(json.dumps(
                {'project': 7, 'api_url': url, 'path': 'locales'},
            ))
            self.assertEqual(run(['pull']), 0)

        self.assertTrue((repo / 'locales' / 'escaped.json').is_file())
        self.assertFalse((self.cwd / 'escaped.json').exists())

    def test_a_crafted_file_name_cannot_escape_the_output_directory(self):
        routes = {
            ('GET', '/api/translations/export/json/'): file_route(
                b'{}', '../../escaped.json', 'application/json',
            ),
        }
        repo = self.in_nested_repo()
        with server(routes) as (url, _):
            (repo / CONFIG_FILENAME).write_text(json.dumps(
                {'project': 7, 'api_url': url, 'path': 'locales'},
            ))
            self.assertEqual(run(['pull', '--language', 'de']), 0)
        self.assertTrue((repo / 'locales' / 'escaped.json').is_file())
        self.assertFalse((self.cwd / 'escaped.json').exists())

    def test_an_api_error_exits_three_not_one(self):
        """A build must be able to tell "could not look" from "found problems"."""
        routes = {
            ('GET', '/api/translations/export/json/'):
                json_route({'message': 'Project with id 7 not found'}, status=404),
        }
        with server(routes) as (url, _):
            self.write_config(project=7, api_url=url)
            self.assertEqual(run(['pull']), 3)


class PushTests(CliTestCase):
    def _routes(self, result=None):
        return {
            ('GET', '/api/translations/projects/7/'): json_route({'data': PROJECT}),
            ('POST', '/api/translations/import/'): json_route({
                'data': result or {'imported': 2, 'updated': 0, 'skipped': 0, 'errors': []},
            }),
        }

    def test_the_language_comes_from_the_filename(self):
        (self.cwd / 'locales').mkdir()
        (self.cwd / 'locales' / 'translations_de.json').write_text('{"a": "A"}')

        with server(self._routes()) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            self.assertEqual(run(['push']), 0)

        upload = next(s for s in seen if s['method'] == 'POST')
        self.assertIn(b'name="language"\r\n\r\nde', upload['body'])
        self.assertIn(b'filename="translations_de.json"', upload['body'])
        self.assertTrue(upload['content_type'].startswith('multipart/form-data; boundary='))

    def test_a_bare_language_filename_also_resolves(self):
        (self.cwd / 'locales').mkdir()
        (self.cwd / 'locales' / 'de.json').write_text('{"a": "A"}')
        with server(self._routes()) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            self.assertEqual(run(['push']), 0)
        upload = next(s for s in seen if s['method'] == 'POST')
        self.assertIn(b'name="language"\r\n\r\nde', upload['body'])

    def test_an_unguessable_filename_fails_that_file_and_says_why(self):
        (self.cwd / 'locales').mkdir()
        (self.cwd / 'locales' / 'strings.json').write_text('{"a": "A"}')
        with server(self._routes()) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            self.assertEqual(run(['push']), 1)
        self.assertFalse(any(s['method'] == 'POST' for s in seen))

    def test_dry_run_is_passed_through_to_the_api(self):
        (self.cwd / 'locales').mkdir()
        (self.cwd / 'locales' / 'translations_de.json').write_text('{"a": "A"}')
        with server(self._routes()) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            self.assertEqual(run(['push', '--dry-run', '--overwrite']), 0)
        upload = next(s for s in seen if s['method'] == 'POST')
        self.assertIn(b'name="dry_run"\r\n\r\ntrue', upload['body'])
        self.assertIn(b'name="overwrite"\r\n\r\ntrue', upload['body'])

    def test_overwrite_defaults_to_false(self):
        (self.cwd / 'locales').mkdir()
        (self.cwd / 'locales' / 'translations_de.json').write_text('{"a": "A"}')
        with server(self._routes()) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            run(['push'])
        upload = next(s for s in seen if s['method'] == 'POST')
        self.assertIn(b'name="overwrite"\r\n\r\nfalse', upload['body'])

    def test_an_explicit_file_outside_the_path_is_accepted(self):
        loose = self.cwd / 'somewhere' / 'translations_de.json'
        loose.parent.mkdir()
        loose.write_text('{"a": "A"}')
        with server(self._routes()) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales')
            self.assertEqual(run(['push', '--file', str(loose)]), 0)
        self.assertTrue(any(s['method'] == 'POST' for s in seen))

    def test_a_named_file_that_does_not_exist_is_a_usage_error(self):
        with server(self._routes()) as (url, _):
            self.write_config(project=7, api_url=url)
            self.assertEqual(run(['push', '--file', 'nope.json']), 2)


class InitTests(CliTestCase):
    ROUTES = {('GET', '/api/translations/projects/7/'): json_route({'data': PROJECT})}

    def test_it_writes_a_config_without_the_api_key_in_it(self):
        with server(self.ROUTES) as (url, _):
            self.assertEqual(
                run(['init', '--project', '7', '--format', 'xliff',
                     '--path', 'locales', '--api-url', url]),
                0,
            )
        written = json.loads((self.cwd / CONFIG_FILENAME).read_text())
        self.assertEqual(written['project'], 7)
        self.assertEqual(written['format'], 'xliff')
        self.assertNotIn('api_key', written)

    def test_it_refuses_to_clobber_an_existing_config(self):
        self.write_config(project=1)
        with server(self.ROUTES) as (url, _):
            self.assertEqual(run(['init', '--project', '7', '--api-url', url]), 2)
        self.assertEqual(json.loads((self.cwd / CONFIG_FILENAME).read_text())['project'], 1)

    def test_force_overwrites(self):
        self.write_config(project=1)
        with server(self.ROUTES) as (url, _):
            self.assertEqual(
                run(['init', '--project', '7', '--api-url', url, '--force']), 0,
            )
        self.assertEqual(json.loads((self.cwd / CONFIG_FILENAME).read_text())['project'], 7)

    def test_it_does_not_write_a_config_for_a_project_it_cannot_reach(self):
        routes = {('GET', '/api/translations/projects/9/'):
                  json_route({'message': 'Forbidden'}, status=403)}
        with server(routes) as (url, _):
            self.assertEqual(run(['init', '--project', '9', '--api-url', url]), 3)
        self.assertFalse((self.cwd / CONFIG_FILENAME).exists())

    def test_an_unknown_format_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit) as caught:
            run(['init', '--project', '7', '--format', 'nonsense'])
        self.assertEqual(caught.exception.code, 2)

    def test_list_shows_the_projects_a_key_can_see(self):
        routes = {('GET', '/api/translations/projects/'):
                  json_route({'data': {'results': [PROJECT]}})}
        with server(routes) as (url, _):
            self.assertEqual(run(['init', '--list', '--api-url', url]), 0)

    def test_list_handles_the_raw_paginated_shape_too(self):
        """The list endpoint returns DRF pagination with no "data" envelope,
        unlike the detail endpoints. Assuming the envelope printed nothing."""
        routes = {('GET', '/api/translations/projects/'):
                  json_route({'count': 1, 'next': None, 'results': [PROJECT]})}
        with server(routes) as (url, _), mock.patch('builtins.print') as printed:
            self.assertEqual(run(['init', '--list', '--api-url', url]), 0)
        printed.assert_any_call('7       Web app')


class UsageTests(CliTestCase):
    def test_no_command_prints_help_and_exits_two(self):
        self.assertEqual(run([]), 2)

    def test_version_is_the_packages_version(self):
        """The release tag is checked against the same __version__."""
        from localizeme_cli import __version__
        from localizeme_cli.client import USER_AGENT

        with self.assertRaises(SystemExit) as caught, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            run(['--version'])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f'localizeme {__version__}')
        self.assertEqual(USER_AGENT, f'localizeme-cli/{__version__}')

    def test_every_format_the_api_supports_is_offered(self):
        """Drift here means a format ships and the CLI cannot write it."""
        self.assertEqual(
            set(commands.FORMAT_EXTENSIONS),
            {'json', 'po', 'ios-strings', 'ios-xcstrings', 'android-xml', 'csv',
             'yaml', 'xliff', 'arb', 'properties'},
        )


class DirectoryPushNamingTests(CliTestCase):
    """A directory of files named like exports, the layout from before templates."""

    ROUTES = {
        ('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT}),
        ('POST', '/api/translations/import/'): json_route({'data': {'imported': 1}}),
    }

    def push_one(self, filename, **settings):
        (self.cwd / 'locales').mkdir()
        (self.cwd / 'locales' / filename).write_text('x')
        with server(self.ROUTES) as (url, seen):
            self.write_config(project=7, api_url=url, path='locales', **settings)
            exit_code = run(['push'])
        return exit_code, uploads(seen)

    def test_a_file_named_for_another_spelling_reaches_its_language(self):
        exit_code, sent = self.push_one('nb.json')
        self.assertEqual(exit_code, 0)
        self.assertEqual(sent[0]['language'], 'no')

    def test_a_regional_code_is_not_split_in_two(self):
        exit_code, sent = self.push_one('strings_pt-BR.xml', format='android-xml')
        self.assertEqual(exit_code, 0)
        self.assertEqual(sent[0]['language'], 'pt-BR')

    def test_a_catalog_needs_no_language_to_be_pushed(self):
        """It carries every language; this used to fail unless --language was passed."""
        exit_code, sent = self.push_one('Localizable.xcstrings', format='ios-xcstrings')
        self.assertEqual(exit_code, 0)
        self.assertEqual(sent[0]['filename'], 'Localizable.xcstrings')
        # The file names each string's language, so none is sent with it.
        self.assertNotIn('language', sent[0])

    def test_a_region_the_project_lacks_is_not_guessed_into_the_base_language(self):
        """British strings must not land on en just because the name starts with it."""
        exit_code, sent = self.push_one('strings_en-GB.xml', format='android-xml')
        self.assertEqual(exit_code, 1)
        self.assertEqual(sent, [])

    def test_a_yml_file_is_found_for_yaml(self):
        exit_code, sent = self.push_one('de.yml', format='yaml')
        self.assertEqual(exit_code, 0)
        self.assertEqual(sent[0]['language'], 'de')


class TemplatePullTests(CliTestCase):
    EXPORT = zip_route({
        'strings_en.xml': '<resources>en</resources>',
        'strings_de.xml': '<resources>de</resources>',
        'strings_pt-BR.xml': '<resources>pt-BR</resources>',
        'strings_no.xml': '<resources>no</resources>',
    })

    def routes(self, export=None, project=MOBILE_PROJECT):
        return {
            ('GET', '/api/translations/projects/7/'): json_route({'data': project}),
            ('GET', '/api/translations/export/android-xml/'): export or self.EXPORT,
        }

    def res(self, directory):
        return (self.cwd / RES / directory / 'strings.xml').read_text()

    def test_an_android_app_gets_the_layout_gradle_reads(self):
        with server(self.routes()) as (url, seen):
            self.write_config(project=7, api_url=url, **ANDROID)
            self.assertEqual(run(['pull']), 0)

        self.assertEqual(self.res('values'), '<resources>en</resources>')
        self.assertEqual(self.res('values-de'), '<resources>de</resources>')
        self.assertEqual(self.res('values-pt-rBR'), '<resources>pt-BR</resources>')
        self.assertEqual(self.res('values-no'), '<resources>no</resources>')
        # The source language went to source_path instead, not to values-en too.
        self.assertFalse((self.cwd / RES / 'values-en').exists())

        export = next(s for s in seen if s['path'].endswith('/export/android-xml/'))
        self.assertEqual(export['query']['language'], ['all'])
        self.assertEqual(export['query']['platform'], ['android'])

    def test_one_language_is_named_for_the_code_asked_for(self):
        """How one set of strings ships as nb here and no somewhere else."""
        export = file_route(b'<resources>no</resources>', 'strings_nb.xml')
        with server(self.routes(export)) as (url, seen):
            self.write_config(project=7, api_url=url, **ANDROID)
            self.assertEqual(run(['pull', '--language', 'nb']), 0)

        self.assertEqual(self.res('values-nb'), '<resources>no</resources>')
        self.assertEqual(seen[-1]['query']['language'], ['nb'])

    def test_the_source_language_on_its_own_goes_to_source_path(self):
        export = file_route(b'<resources>en</resources>', 'strings_en.xml')
        with server(self.routes(export)) as (url, _):
            self.write_config(project=7, api_url=url, **ANDROID)
            self.assertEqual(run(['pull', '--language', 'en']), 0)
        self.assertEqual(self.res('values'), '<resources>en</resources>')

    def test_codes_renames_a_language_in_the_path(self):
        with server(self.routes()) as (url, _):
            self.write_config(project=7, api_url=url, codes={'no': 'nb'}, **ANDROID)
            self.assertEqual(run(['pull']), 0)
        self.assertEqual(self.res('values-nb'), '<resources>no</resources>')
        self.assertFalse((self.cwd / RES / 'values-no').exists())

    def test_codes_naming_no_language_of_the_project_is_a_usage_error(self):
        with server(self.routes()) as (url, seen):
            self.write_config(project=7, api_url=url, codes={'xx': 'yy'}, **ANDROID)
            self.assertEqual(run(['pull']), 2)
        self.assertFalse(any('/export/' in s['path'] for s in seen))

    def test_source_path_without_a_source_language_stops_before_the_export(self):
        project = dict(MOBILE_PROJECT, source_language=None, languages=[
            dict(row, is_source=False) for row in MOBILE_PROJECT['languages']
        ])
        with server(self.routes(project=project)) as (url, seen):
            self.write_config(project=7, api_url=url, **ANDROID)
            self.assertEqual(run(['pull']), 2)
        self.assertFalse(any('/export/' in s['path'] for s in seen))
        self.assertFalse((self.cwd / RES).exists())

    def test_an_export_file_for_no_known_language_fails_only_that_file(self):
        export = zip_route({
            'strings_de.xml': '<resources>de</resources>',
            'strings_xx.xml': '<resources>?</resources>',
        })
        with server(self.routes(export)) as (url, _):
            self.write_config(project=7, api_url=url, **ANDROID)
            self.assertEqual(run(['pull']), 1)
        self.assertEqual(self.res('values-de'), '<resources>de</resources>')

    def test_paths_are_relative_to_the_config_file_not_the_shell(self):
        with server(self.routes()) as (url, _):
            self.write_config(project=7, api_url=url, **ANDROID)
            nested = self.cwd / 'app'
            nested.mkdir()
            os.chdir(nested)
            self.assertEqual(run(['pull']), 0)
        self.assertTrue((self.cwd / RES / 'values-de' / 'strings.xml').is_file())
        self.assertFalse((self.cwd / 'app' / RES).exists())

    def test_a_path_on_the_command_line_keeps_the_files_source_path(self):
        """--path is relative to the shell; source_path is still relative to the file."""
        with server(self.routes()) as (url, _):
            self.write_config(project=7, api_url=url, **ANDROID)
            nested = self.cwd / 'app'
            nested.mkdir()
            os.chdir(nested)
            exit_code = run([
                'pull', '--path', 'src/main/res/values-{android_code}/strings.xml',
            ])
            self.assertEqual(exit_code, 0)
        self.assertEqual(self.res('values'), '<resources>en</resources>')
        self.assertEqual(self.res('values-de'), '<resources>de</resources>')
        self.assertFalse((self.cwd / 'app' / RES).exists())

    def test_a_directory_on_the_command_line_leaves_source_path_behind(self):
        """source_path means nothing beside a directory, and no flag can unset it."""
        with server(self.routes()) as (url, _):
            self.write_config(project=7, api_url=url, codes={'no': 'nb'}, **ANDROID)
            self.assertEqual(run(['pull', '--path', 'raw']), 0)
        self.assertTrue((self.cwd / 'raw' / 'strings_de.xml').is_file())
        self.assertFalse((self.cwd / RES).exists())

    def test_locale_spells_a_region_with_an_underscore(self):
        """Flutter, gettext and Java want app_pt_BR, not app_pt-BR."""
        routes = {
            ('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT}),
            ('GET', '/api/translations/export/arb/'): zip_route({
                'app_en.arb': '{"a": "A"}',
                'app_pt-BR.arb': '{"a": "Á"}',
            }),
        }
        with server(routes) as (url, _):
            self.write_config(project=7, api_url=url, format='arb', path='lib/l10n/app_{locale}.arb')
            self.assertEqual(run(['pull']), 0)
        self.assertEqual(
            sorted(p.name for p in (self.cwd / 'lib' / 'l10n').iterdir()),
            ['app_en.arb', 'app_pt_BR.arb'],
        )

    def test_a_web_app_gets_one_plain_file_per_language(self):
        routes = {
            ('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT}),
            ('GET', '/api/translations/export/json/'): zip_route({
                'translations_en.json': '{"a": "A"}',
                'translations_pt-BR.json': '{"a": "Á"}',
            }),
        }
        with server(routes) as (url, _):
            self.write_config(project=7, api_url=url, path='src/locales/{code}.json')
            self.assertEqual(run(['pull']), 0)
        self.assertEqual(
            sorted(p.name for p in (self.cwd / 'src' / 'locales').iterdir()),
            ['en.json', 'pt-BR.json'],
        )


class TemplatePushTests(CliTestCase):
    ROUTES = {
        ('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT}),
        ('POST', '/api/translations/import/'): json_route({
            'data': {'imported': 1, 'updated': 0, 'skipped': 0, 'errors': []},
        }),
    }

    def res(self, directory, content=None):
        path = self.cwd / RES / directory / 'strings.xml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content or f'<resources>{directory}</resources>')
        return path

    def push(self, *flags, **settings):
        with server(self.ROUTES) as (url, seen):
            self.write_config(project=7, api_url=url, **{**ANDROID, **settings})
            exit_code = run(['push', *flags])
        return exit_code, uploads(seen)

    def test_each_resource_directory_is_pushed_as_its_language(self):
        self.res('values')
        self.res('values-de')
        self.res('values-pt-rBR')
        self.res('values-nb')
        self.res('values-night')  # a qualifier, not a language

        exit_code, sent = self.push()

        self.assertEqual(exit_code, 0)
        self.assertEqual(sorted(u['language'] for u in sent), ['de', 'en', 'no', 'pt-BR'])
        self.assertTrue(all(u['platform'] == 'android' for u in sent))
        self.assertTrue(all(u['format'] == 'android-xml' for u in sent))

    def test_the_same_strings_under_two_spellings_are_pushed_once(self):
        self.res('values-no', '<resources>same</resources>')
        self.res('values-nb', '<resources>same</resources>')
        exit_code, sent = self.push()
        self.assertEqual(exit_code, 0)
        self.assertEqual([u['language'] for u in sent], ['no'])

    def test_different_strings_under_two_spellings_are_not_guessed_between(self):
        self.res('values')
        self.res('values-no', '<resources>one</resources>')
        self.res('values-nb', '<resources>other</resources>')
        exit_code, sent = self.push()
        self.assertEqual(exit_code, 1)
        self.assertEqual([u['language'] for u in sent], ['en'])

    def test_a_values_en_override_beside_the_source_is_left_alone(self):
        """values/ is where the source lives; values-en/ is Android's own override."""
        self.res('values', '<resources>the source</resources>')
        self.res('values-en', '<resources>a few English-only strings</resources>')
        self.res('values-de')
        exit_code, sent = self.push()
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            {u['language']: u['content'] for u in sent},
            {'en': '<resources>the source</resources>',
             'de': '<resources>values-de</resources>'},
        )

    def test_locale_is_read_back_to_the_projects_code(self):
        l10n = self.cwd / 'lib' / 'l10n'
        l10n.mkdir(parents=True)
        (l10n / 'app_pt_BR.arb').write_text('{}')
        (l10n / 'app_en.arb').write_text('{}')
        with server(self.ROUTES) as (url, seen):
            self.write_config(project=7, api_url=url, format='arb', path='lib/l10n/app_{locale}.arb')
            self.assertEqual(run(['push']), 0)
        self.assertEqual(sorted(u['language'] for u in uploads(seen)), ['en', 'pt-BR'])

    def test_language_picks_one_file(self):
        self.res('values')
        self.res('values-de')
        exit_code, sent = self.push('--language', 'de')
        self.assertEqual(exit_code, 0)
        self.assertEqual([u['language'] for u in sent], ['de'])

    def test_a_named_file_takes_its_language_from_the_template(self):
        self.res('values')
        path = self.res('values-pt-rBR')
        exit_code, sent = self.push('--file', str(path))
        self.assertEqual(exit_code, 0)
        self.assertEqual([u['language'] for u in sent], ['pt-BR'])

    def test_nothing_to_push_is_a_usage_error(self):
        exit_code, sent = self.push()
        self.assertEqual(exit_code, 2)
        self.assertEqual(sent, [])


class CatalogTests(CliTestCase):
    """An .xcstrings catalog carries every language in the one file."""

    def test_pull_writes_the_catalog_where_the_path_says(self):
        routes = {('GET', '/api/translations/export/ios-xcstrings/'):
                  file_route(b'{"strings": {}}', 'Localizable.xcstrings', 'application/json')}
        with server(routes) as (url, seen):
            self.write_config(project=7, api_url=url, **IOS)
            self.assertEqual(run(['pull']), 0)
        written = self.cwd / 'ios' / 'App' / 'Localizable.xcstrings'
        self.assertEqual(written.read_bytes(), b'{"strings": {}}')
        self.assertEqual(seen[0]['query']['platform'], ['ios'])

    def test_push_sends_the_catalog_named_by_the_path(self):
        catalog = self.cwd / 'ios' / 'App' / 'Localizable.xcstrings'
        catalog.parent.mkdir(parents=True)
        catalog.write_text('{"strings": {}}')
        routes = {
            ('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT}),
            ('POST', '/api/translations/import/'): json_route({'data': {'imported': 3}}),
        }
        with server(routes) as (url, seen):
            self.write_config(project=7, api_url=url, **IOS)
            self.assertEqual(run(['push']), 0)
        self.assertEqual(uploads(seen)[0]['filename'], 'Localizable.xcstrings')

    def test_a_catalog_path_cannot_hold_a_placeholder(self):
        self.write_config(project=7, format='ios-xcstrings', path='ios/{code}.xcstrings')
        self.assertEqual(run(['pull']), 2)


class TargetsTests(CliTestCase):
    def routes(self):
        return {
            ('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT}),
            ('GET', '/api/translations/export/android-xml/'): zip_route({
                'strings_en.xml': '<resources>en</resources>',
                'strings_de.xml': '<resources>de</resources>',
            }),
            ('GET', '/api/translations/export/ios-xcstrings/'):
                file_route(b'{"strings": {}}', 'Localizable.xcstrings', 'application/json'),
        }

    def targets(self):
        return [dict(ANDROID, name='android'), dict(IOS, name='ios')]

    def test_each_target_pulls_its_own_format_and_platform(self):
        with server(self.routes()) as (url, seen):
            self.write_config(project=7, api_url=url, targets=self.targets())
            self.assertEqual(run(['pull']), 0)

        self.assertTrue((self.cwd / RES / 'values-de' / 'strings.xml').is_file())
        self.assertTrue((self.cwd / 'ios' / 'App' / 'Localizable.xcstrings').is_file())
        platforms = {
            s['path']: s['query']['platform'] for s in seen if '/export/' in s['path']
        }
        self.assertEqual(platforms, {
            '/api/translations/export/android-xml/': ['android'],
            '/api/translations/export/ios-xcstrings/': ['ios'],
        })

    def test_a_failed_export_leaves_every_file_as_it_was(self):
        """Android arrives, iOS does not: the build must not get half of each pull."""
        old = self.cwd / RES / 'values-de' / 'strings.xml'
        old.parent.mkdir(parents=True)
        old.write_text('<resources>old</resources>')
        routes = self.routes()
        del routes[('GET', '/api/translations/export/ios-xcstrings/')]
        with server(routes) as (url, _):
            self.write_config(project=7, api_url=url, targets=self.targets())
            self.assertEqual(run(['pull']), 3)
        self.assertEqual(old.read_text(), '<resources>old</resources>')
        self.assertFalse((self.cwd / RES / 'values').exists())

    def test_target_picks_one_by_name(self):
        with server(self.routes()) as (url, seen):
            self.write_config(project=7, api_url=url, targets=self.targets())
            self.assertEqual(run(['pull', '--target', 'ios']), 0)
        exports = [s['path'] for s in seen if '/export/' in s['path']]
        self.assertEqual(exports, ['/api/translations/export/ios-xcstrings/'])
        self.assertFalse((self.cwd / RES).exists())

    def test_an_unknown_target_is_a_usage_error(self):
        self.write_config(project=7, targets=self.targets())
        self.assertEqual(run(['pull', '--target', 'web']), 2)

    def test_a_flag_for_one_target_needs_one_in_play(self):
        """--platform ios across both would write iOS values into the Android files."""
        self.write_config(project=7, targets=self.targets())
        self.assertEqual(run(['pull', '--platform', 'ios']), 2)

    def test_a_flag_applies_once_one_target_is_picked(self):
        with server(self.routes()) as (url, seen):
            self.write_config(project=7, api_url=url, targets=self.targets())
            self.assertEqual(run(['pull', '--target', 'ios', '--platform', 'all']), 0)
        self.assertEqual(seen[-1]['query']['platform'], ['all'])

    def test_push_plans_every_target_before_sending_anything(self):
        """The iOS catalog is missing, so the Android files must not go either."""
        (self.cwd / RES / 'values-de').mkdir(parents=True)
        (self.cwd / RES / 'values-de' / 'strings.xml').write_text('<resources/>')
        routes = {
            ('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT}),
            ('POST', '/api/translations/import/'): json_route({'data': {'imported': 1}}),
        }
        with server(routes) as (url, seen):
            self.write_config(project=7, api_url=url, targets=self.targets())
            self.assertEqual(run(['push']), 2)
        self.assertEqual(uploads(seen), [])

    def test_settings_beside_targets_are_refused(self):
        self.write_config(project=7, format='json', targets=self.targets())
        self.assertEqual(run(['pull']), 2)

    def test_a_target_needs_a_format_and_a_path(self):
        self.write_config(project=7, targets=[{'name': 'web', 'format': 'json'}])
        self.assertEqual(run(['pull']), 2)

    def test_target_names_are_unique(self):
        self.write_config(project=7, targets=[dict(IOS, name='app'), dict(ANDROID, name='app')])
        self.assertEqual(run(['pull']), 2)

    def test_an_unknown_setting_in_a_target_is_named(self):
        self.write_config(project=7, targets=[dict(IOS, fomat='json')])
        self.assertEqual(run(['pull']), 2)


class InitLayoutTests(CliTestCase):
    ROUTES = {('GET', '/api/translations/projects/7/'): json_route({'data': MOBILE_PROJECT})}

    def test_a_template_and_source_path_are_written_as_given(self):
        with server(self.ROUTES) as (url, _):
            self.assertEqual(run([
                'init', '--project', '7', '--api-url', url, '--format', 'android-xml',
                '--platform', 'android', '--path', ANDROID['path'],
                '--source-path', ANDROID['source_path'],
            ]), 0)
        written = json.loads((self.cwd / CONFIG_FILENAME).read_text())
        self.assertEqual(written['path'], ANDROID['path'])
        self.assertEqual(written['source_path'], ANDROID['source_path'])
        self.assertEqual(written['platform'], 'android')

    def test_a_new_path_leaves_the_old_paths_settings_behind(self):
        """source_path and codes belonged to the template; a directory has no use for them."""
        with server(self.ROUTES) as (url, _):
            self.write_config(project=7, codes={'no': 'nb'}, **ANDROID)
            self.assertEqual(run([
                'init', '--force', '--project', '7', '--api-url', url,
                '--format', 'json', '--path', 'locales',
            ]), 0)
        written = json.loads((self.cwd / CONFIG_FILENAME).read_text())
        self.assertEqual(written['path'], 'locales')
        self.assertNotIn('source_path', written)
        self.assertNotIn('codes', written)

    def test_without_a_new_path_they_are_kept(self):
        with server(self.ROUTES) as (url, _):
            self.write_config(project=7, codes={'no': 'nb'}, **ANDROID)
            self.assertEqual(run(['init', '--force', '--project', '7', '--api-url', url]), 0)
        written = json.loads((self.cwd / CONFIG_FILENAME).read_text())
        self.assertEqual(written['source_path'], ANDROID['source_path'])
        self.assertEqual(written['codes'], {'no': 'nb'})

    def test_an_unknown_placeholder_is_refused_before_anything_is_fetched(self):
        with server(self.ROUTES) as (url, seen):
            self.assertEqual(run([
                'init', '--project', '7', '--api-url', url, '--path', 'locales/{lang}.json',
            ]), 2)
        self.assertEqual(seen, [])
        self.assertFalse((self.cwd / CONFIG_FILENAME).exists())

    def test_one_file_for_one_language_needs_the_language(self):
        self.assertEqual(
            run(['init', '--project', '7', '--format', 'json', '--path', 'messages.json']), 2,
        )

    def test_source_path_needs_a_project_with_a_source_language(self):
        routes = {('GET', '/api/translations/projects/7/'): json_route({'data': PROJECT})}
        with server(routes) as (url, _):
            self.assertEqual(run([
                'init', '--project', '7', '--api-url', url, '--format', 'android-xml',
                '--path', ANDROID['path'], '--source-path', ANDROID['source_path'],
            ]), 2)
        self.assertFalse((self.cwd / CONFIG_FILENAME).exists())


if __name__ == '__main__':
    unittest.main()

"""The commands themselves.

Output is deliberately plain: no colour, no spinners, no cursor tricks. Most of
these runs happen in CI where all of that becomes escape-code noise in a log
somebody is reading to work out why the build failed.
"""

import io
import json
import os
import re
import zipfile
from pathlib import Path, PurePath

from .client import ApiError, Client
from .config import (
    CONFIG_FILENAME,
    Config,
    ConfigError,
    Target,
    check_single_file_language,
    check_target,
    select_targets,
)
from .layout import (
    FORMAT_EXTENSIONS,
    FORMAT_SUFFIXES,
    MULTI_LANGUAGE_FORMATS,
    PLACEHOLDERS,
    ProjectLanguages,
    TemplateError,
    language_in_name,
)


class CommandError(Exception):
    """A command cannot continue. The message is meant for a human."""


def _client(config: Config) -> Client:
    return Client(config.api_url, config.require_api_key())


def _payload_data(payload):
    """The body of a response, whichever envelope it arrived in.

    Most endpoints wrap their result as ``{"success", "message", "data"}``, but
    the ViewSet list endpoints return DRF's paginated ``{"count", "results"}``
    directly. Tolerating both keeps the CLI working across that seam instead of
    silently reading ``None``.
    """
    if isinstance(payload, dict) and 'data' in payload:
        return payload['data']
    return payload


def _project_languages(client: Client, project: int) -> ProjectLanguages:
    payload = client.get_json(f'/translations/projects/{project}/')
    return ProjectLanguages.from_project(_payload_data(payload) or {})


def _header(headers: dict, name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ''


_FILENAME_IN_DISPOSITION = re.compile(r'filename="?([^";]+)"?')


def _filename_from(headers: dict, fallback: str) -> str:
    match = _FILENAME_IN_DISPOSITION.search(_header(headers, 'Content-Disposition'))
    # Only ever the name: a crafted header must not steer the write elsewhere.
    name = PurePath(match.group(1)).name if match else ''
    return name or fallback


def _is_zip(headers: dict) -> bool:
    return 'zip' in _header(headers, 'Content-Type').lower()


def _display(path: Path) -> str:
    """A path as it reads best in a log: relative to where the command ran."""
    try:
        return os.path.relpath(path)
    except ValueError:  # on another drive, on Windows
        return str(path)


def _check_source(target: Target, languages: ProjectLanguages, project: int) -> None:
    if target.source_path and languages.source is None:
        raise CommandError(
            f'"source_path" says where the source language goes, but project {project} '
            f'has no source language set. Choose one in the dashboard.'
        )


def _template_names(target: Target, languages: ProjectLanguages, project: int) -> dict:
    """Check a template target against the project; return its "codes" by language.

    Runs before anything is fetched or written, so a typo in the config stops
    the command rather than leaving half a pull behind.
    """
    names = {}
    for key, value in target.codes.items():
        code = languages.resolve(key)
        if code is None:
            raise CommandError(
                f'"codes" names {key}, which is not a language in project {project}. '
                f'It has: {", ".join(languages.codes)}.'
            )
        names[code] = value
    _check_source(target, languages, project)
    if target.language and languages.resolve(target.language) is None:
        raise CommandError(
            f'{target.language} is not a language in project {project}. '
            f'It has: {", ".join(languages.codes)}.'
        )
    return names


# --- pull ------------------------------------------------------------------
#
# Each target's files are fetched into memory and only written once every
# export has arrived, so an API that fails part way through leaves the files
# as they were, rather than some from this pull and some from the last for a
# build to compile together.

def _export(client: Client, project: int, target: Target, language: str, args):
    params = {
        'project_id': project,
        'language': language,
        'platform': target.platform,
    }
    if args.status:
        params['status'] = args.status
    # Repeatable on the command line, one comma-separated value on the wire —
    # the same parameter the dashboard's export sends.
    groups = getattr(args, 'group', None)
    if groups:
        params['groups'] = ','.join(groups)
    return client.get_bytes(f'/translations/export/{target.format}/', params)


def _fetch_directory(client, project, target: Target, args) -> list[tuple[Path, bytes]]:
    """Files keep the names the API gives them."""
    destination = target.location
    # 'all' is the default because a partial pull is the surprising one: a build
    # that ships one language because a flag was forgotten is worse than a slow
    # export.
    language = target.language or 'all'
    body, headers = _export(client, project, target, language, args)

    if _is_zip(headers):
        # One file per language, which is what an all-languages export returns
        # for every format except the multi-language ones.
        files = []
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for name in archive.namelist():
                # Guard against a path escaping the destination directory.
                safe = Path(name).name
                if safe:
                    files.append((destination / safe, archive.read(name)))
        return files
    fallback = f'translations_{language}{FORMAT_EXTENSIONS.get(target.format, "")}'
    return [(destination / _filename_from(headers, fallback), body)]


def _fetch_file(client, project, target: Target, args) -> list[tuple[Path, bytes]]:
    """The path names the file, whatever the API calls it."""
    # A catalog carries every language whatever is asked for, but the export
    # still wants a language on the request.
    catalog = target.format in MULTI_LANGUAGE_FORMATS
    language = 'all' if catalog else target.language
    body, _ = _export(client, project, target, language, args)
    return [(target.location, body)]


def _exported_languages(body: bytes, headers: dict, languages: ProjectLanguages):
    """Split an all-languages export into ``(code, content)``, plus what would not split.

    Export names each file for its language (``strings_de.xml``), which is how
    a file finds its way back to the language it holds.
    """
    if _is_zip(headers):
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = [
                (name, archive.read(name))
                for name in archive.namelist() if not name.endswith('/')
            ]
    else:
        # Every language comes back as a zip, even for a project with one; this
        # only keeps a single file from being lost if that ever changes.
        entries = [(_filename_from(headers, ''), body)]

    found, unplaced = [], []
    for name, content in entries:
        code = language_in_name(name, languages) if name else None
        if code is None:
            unplaced.append(name or 'the export')
        else:
            found.append((code, content))
    return found, unplaced


def _fetch_template(client, project, target: Target, args, languages, names):
    """One file per language, each named by filling the template in."""
    failures = []
    if target.language:
        code = languages.resolve(target.language)
        body, _ = _export(client, project, target, target.language, args)
        # Named for the code asked for, so `--language nb` writes nb, not no.
        exported = [(code, names.get(code, target.language), body)]
    else:
        body, headers = _export(client, project, target, 'all', args)
        found, unplaced = _exported_languages(body, headers, languages)
        exported = [(code, names.get(code, code), content) for code, content in found]
        for name in unplaced:
            failures.append(
                f'{name} in the export: could not tell which language it holds, '
                f'so it was not written.'
            )

    files: list[tuple[Path, bytes]] = []
    claimed: dict[Path, str] = {}
    for code, spelling, content in exported:
        try:
            if target.source_path and code == languages.source:
                path = target.source_file
            else:
                path = target.template.render(spelling)
        except TemplateError as exc:
            failures.append(str(exc))
            continue
        if path in claimed:
            failures.append(
                f'{claimed[path]} and {code} would both be written to {_display(path)}. '
                f'Give one of them its own name under "codes".'
            )
            continue
        claimed[path] = code
        files.append((path, content))
    return files, failures


def pull(config: Config, args) -> int:
    client = _client(config)
    project = config.require_project()
    targets = select_targets(config, args)

    # Templates are checked against the project before anything is fetched.
    languages = None
    names = {}
    if any(target.layout == 'template' for target in targets):
        languages = _project_languages(client, project)
        for index, target in enumerate(targets):
            if target.layout == 'template':
                names[index] = _template_names(target, languages, project)

    fetched: list[tuple[Target, list]] = []
    failures: list[str] = []
    for index, target in enumerate(targets):
        if target.layout == 'template':
            files, failed = _fetch_template(
                client, project, target, args, languages, names[index],
            )
            failures.extend(failed)
        elif target.layout == 'file':
            files = _fetch_file(client, project, target, args)
        else:
            files = _fetch_directory(client, project, target, args)
        fetched.append((target, files))

    groups = getattr(args, 'group', None)
    scope = f' in {", ".join(groups)}' if groups else ''
    for target, files in fetched:
        for path, content in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for path in sorted(path for path, _ in files):
            print(f'wrote {_display(path)}')
        print(f'{len(files)} file(s) from project {project} as {target.format}{scope}')

    for failure in failures:
        print(f'error: {failure}')
    return 1 if failures else 0


# --- push ------------------------------------------------------------------

def _placeholder_lookup(target: Target, languages: ProjectLanguages, names: dict) -> dict:
    """For each placeholder in the template: what it reads as -> language code.

    Built from every code the project's languages answer to, and from "codes",
    so values-nb/ and values-iw/ find Norwegian and Hebrew.
    """
    spellings = [(spelling, code) for code, spelling in names.items()]
    spellings += languages.spellings()
    lookup = {}
    for placeholder in set(target.template.placeholders):
        transform = PLACEHOLDERS[placeholder]
        table = lookup[placeholder] = {}
        for spelling, code in spellings:
            table.setdefault(transform(spelling).casefold(), code)
    return lookup


def _language_from_values(values: dict, lookup: dict) -> str | None:
    codes = {lookup[name].get(value.casefold()) for name, value in values.items()}
    if len(codes) == 1 and None not in codes:
        return codes.pop()
    return None


def _language_for_file(path: Path, target: Target, args, languages, lookup) -> str | None:
    """Which language one named file holds.

    --language wins, since it was typed next to the file. Then the template
    the file sits in, then "language" from the config, then the file's name.
    """
    if getattr(args, 'language', None):
        return args.language
    if target.layout == 'template':
        source = target.source_file
        if source is not None and path.resolve() == source.resolve():
            return languages.source
        values = target.template.match(path)
        if values:
            found = _language_from_values(values, lookup)
            if found:
                return found
    if target.language:
        return target.language
    return language_in_name(path.name, languages)


def _files_in_directory(target: Target) -> list[Path]:
    directory = target.location
    if not directory.is_dir():
        raise CommandError(
            f'{_display(directory)} is not a directory. Pass --file, or set "path" in '
            f'{CONFIG_FILENAME}.'
        )
    suffixes = FORMAT_SUFFIXES[target.format]
    found = sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in suffixes
    )
    if not found:
        raise CommandError(f'No {" or ".join(suffixes)} files in {_display(directory)} to push.')
    return found


def _files_for_template(target: Target, languages, lookup, project):
    """Every file the template describes, by the language each one holds.

    A file in a directory that is not a language, like values-night/, is
    skipped with a note: Android keeps plenty of those, and none are failures.
    """
    found: dict[str, list[Path]] = {}
    source = target.source_file
    if source is not None and source.is_file():
        found.setdefault(languages.source, []).append(source)
    for path, values in target.template.find():
        if source is not None and path.resolve() == source.resolve():
            continue
        code = _language_from_values(values, lookup)
        if code is None:
            shown = ', '.join(sorted(set(values.values())))
            print(f'skipped {_display(path)}: {shown} is not a language in project {project}')
            continue
        if source is not None and code == languages.source:
            # source_path is where the source language lives, as pull writes it.
            # A values-en/ beside values/ is Android's override for English
            # devices, often a handful of strings, not a second copy to weigh
            # against the first.
            print(
                f'skipped {_display(path)}: {languages.describe(code)} is pushed '
                f'from {_display(source)}'
            )
            continue
        found.setdefault(code, []).append(path)

    if target.language:
        wanted = languages.resolve(target.language)
        found = {code: paths for code, paths in found.items() if code == wanted}
    if not found:
        scope = f' for {target.language}' if target.language else ''
        raise CommandError(f'No files match {target.path}{scope} to push.')

    planned, failures = [], []
    for code, paths in found.items():
        if len(paths) > 1:
            # Android projects often keep values-he/ and values-iw/ side by side
            # for old devices. Identical copies are one push; different ones are
            # a question only a person can answer.
            shown = ' and '.join(_display(p) for p in paths)
            language = languages.describe(code)
            if len({p.read_bytes() for p in paths}) > 1:
                failures.append(
                    f'{shown} are both {language}, with different strings. '
                    f'Push the right one with --file.'
                )
                continue
            print(f'{shown} are both {language}, with the same strings; '
                  f'pushing {_display(paths[0])}')
        planned.append((paths[0], code))
    return sorted(planned, key=lambda item: str(item[0])), failures


def _plan_push(target: Target, args, languages: ProjectLanguages, project: int):
    """``[(path, language)]`` to upload for one target, plus the files that cannot be.

    The language is None for a catalog, which carries its own.
    """
    names = _template_names(target, languages, project) if target.layout == 'template' else {}
    lookup = _placeholder_lookup(target, languages, names) if target.layout == 'template' else {}
    catalog = target.format in MULTI_LANGUAGE_FORMATS

    if getattr(args, 'file', None):
        paths = [Path(f) for f in args.file]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            raise CommandError(f'No such file(s): {", ".join(missing)}')
    elif target.layout == 'file':
        path = target.location
        if not path.is_file():
            raise CommandError(f'No such file: {_display(path)}')
        paths = [path]
    elif target.layout == 'directory':
        paths = _files_in_directory(target)
    else:
        return _files_for_template(target, languages, lookup, project)

    planned, failures = [], []
    for path in paths:
        if catalog:
            # A catalog names the language of every string in it, so it is sent
            # without one of its own.
            planned.append((path, None))
            continue
        language = _language_for_file(path, target, args, languages, lookup)
        if language is None:
            failures.append(
                f'{_display(path)}: could not tell which language this is for. '
                f'Pass --language, or name it like translations_de{path.suffix}. '
                f'Enabled: {", ".join(sorted(languages.codes))}.'
            )
            continue
        planned.append((path, language))
    return planned, failures


def push(config: Config, args) -> int:
    client = _client(config)
    project = config.require_project()
    targets = select_targets(config, args)

    languages = _project_languages(client, project)
    if not languages.codes:
        raise CommandError(f'Project {project} has no languages enabled yet.')

    # Every target is worked out before anything is sent, so a mistake in the
    # last one stops the command instead of arriving after the first has
    # already been imported.
    uploads, failures = [], []
    for target in targets:
        planned, failed = _plan_push(target, args, languages, project)
        uploads.extend((target, path, language) for path, language in planned)
        failures.extend(failed)

    totals = {'imported': 0, 'updated': 0, 'skipped': 0}
    sent = 0
    for target, path, language in uploads:
        fields = {
            'project_id': str(project),
            'format': target.format,
            'language': language,
            'platform': target.platform,
            'overwrite': 'true' if args.overwrite else 'false',
            'dry_run': 'true' if args.dry_run else 'false',
        }
        if args.status:
            fields['status'] = args.status

        try:
            payload = client.post_file(
                '/translations/import/', fields, path.name, path.read_bytes(),
            )
        except ApiError as exc:
            failures.append(f'{_display(path)}: {exc}')
            continue

        sent += 1
        data = payload.get('data') or {}
        for field in totals:
            totals[field] += data.get(field, 0)
        errors = data.get('errors') or []
        label = 'all languages' if target.format in MULTI_LANGUAGE_FORMATS else language
        print(
            f'{_display(path)} [{label}] '
            f'imported {data.get("imported", 0)}, '
            f'updated {data.get("updated", 0)}, '
            f'skipped {data.get("skipped", 0)}'
            + (f', {len(errors)} parse error(s)' if errors else '')
        )
        for error in errors[:5]:
            print(f'    {error.get("key") or "-"}: {error.get("error")}')

    prefix = 'would import' if args.dry_run else 'imported'
    print(
        f'{prefix} {totals["imported"]}, updated {totals["updated"]}, '
        f'skipped {totals["skipped"]} across {sent} file(s)'
    )
    if args.dry_run:
        print('dry run — nothing was written')

    for failure in failures:
        print(f'error: {failure}')
    return 1 if failures else 0


# --- init ------------------------------------------------------------------

def init(config: Config, args) -> int:
    config_path = Path.cwd() / CONFIG_FILENAME
    if config_path.exists() and not args.force:
        raise CommandError(f'{config_path} already exists. Pass --force to overwrite it.')

    project = config.project
    if project is None:
        raise CommandError(
            'Which project? Pass --project <id>. '
            '`localizeme init --list` shows the ones your key can see.'
        )

    # Start from what an existing file says when it describes one set of files,
    # so `init --force --format po` changes the format and keeps the rest. A
    # file with several targets is not something init can write, so it starts
    # over.
    base = Target() if config.has_targets else config.targets[0]
    # "source_path" and "codes" describe the path they were written for, so a
    # new --path starts without them rather than keeping ones that no longer
    # fit it.
    new_path = args.path is not None and args.path != base.path
    target = Target(
        format=args.format or base.format,
        path=args.path or base.path,
        platform=args.platform or base.platform,
        language=args.language or base.language,
        source_path=args.source_path or (None if new_path else base.source_path),
        codes={} if new_path else dict(base.codes),
        root=Path.cwd(),
    )
    check_target(target, 'the command line')
    check_single_file_language(target)

    # Confirm the key can actually reach the project before writing a file that
    # claims it can — a config that looks right and fails later is worse than an
    # error now.
    client = _client(config)
    payload = _payload_data(client.get_json(f'/translations/projects/{project}/')) or {}
    name = payload.get('name') or str(project)
    _check_source(target, ProjectLanguages.from_project(payload), project)

    settings = {
        'project': project,
        'format': target.format,
        'path': target.path,
    }
    if config.api_url != Config().api_url:
        settings['api_url'] = config.api_url
    if target.platform != 'all':
        settings['platform'] = target.platform
    if target.language:
        settings['language'] = target.language
    if target.source_path:
        settings['source_path'] = target.source_path
    if target.codes:
        settings['codes'] = target.codes

    config_path.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
    print(f'wrote {config_path} for project {project} ({name})')
    print(f'Keep your API key out of it — the CLI reads LOCALIZEME_API_KEY from the environment.')
    return 0


def list_projects(config: Config, args) -> int:
    client = _client(config)
    data = _payload_data(client.get_json('/translations/projects/'))
    projects = data.get('results') if isinstance(data, dict) else data
    for project in projects or []:
        print(f'{project.get("id"):<8}{project.get("name")}')
    if not projects:
        print('no projects visible to this key')
    return 0


def resolve(name: str):
    """Command name -> handler, so main() holds no dispatch table of its own."""
    handlers = {
        'pull': pull,
        'push': push,
        'init': init,
    }
    try:
        return handlers[name]
    except KeyError:
        raise ConfigError(f'Unknown command {name!r}') from None

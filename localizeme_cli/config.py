"""Where settings come from, and in what order.

Precedence is flag, then environment, then config file, then default. That order
is what makes one config file work everywhere: it is committed with the project
and holds the boring facts (which project, which format, where files live),
while the API key arrives from the environment and never goes near the repo.

The file describes one set of translation files at its top level, or several
under "targets". A repo holding an Android app and an iOS app has two, each
with its own format, layout and platform. Relative paths in the file resolve
against the directory the file is in, so a command run from a subdirectory
writes where the file says rather than under wherever the shell happened to be.
"""

import json
import os
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path

from .layout import (
    FORMAT_EXTENSIONS,
    MULTI_LANGUAGE_FORMATS,
    PLACEHOLDER_NAMES,
    PathTemplate,
    TemplateError,
    layout_of,
)

CONFIG_FILENAME = 'localizeme.json'

DEFAULT_API_URL = 'https://api.localizeme.app/api'

ENV_API_KEY = 'LOCALIZEME_API_KEY'
ENV_API_URL = 'LOCALIZEME_API_URL'
ENV_PROJECT = 'LOCALIZEME_PROJECT'

# Settings that describe one set of translation files. At the top level they
# describe the only one; under "targets" each entry carries its own.
TARGET_KEYS = {'name', 'format', 'path', 'platform', 'language', 'source_path', 'codes'}

# Keys we recognise at the top level. Anything else is a typo worth naming
# rather than ignoring — a silently misspelled "fomat" is a confusing morning.
KNOWN_KEYS = {'project', 'api_url', 'targets'} | (TARGET_KEYS - {'name'})

# Flags that describe one target, so they need exactly one in play.
TARGET_FLAGS = (
    ('format', '--format'),
    ('path', '--path'),
    ('platform', '--platform'),
    ('source_path', '--source-path'),
)


class ConfigError(Exception):
    """Configuration is missing or unusable. The message is meant for a human."""


@dataclass(frozen=True)
class Target:
    """One set of translation files: its format, where it lives, whose values.

    Frozen, so the template worked out from ``path`` can be kept: a change makes
    a new Target with ``dataclasses.replace`` and a new template with it.
    """

    format: str = 'json'
    path: str = 'locales'
    platform: str = 'all'
    language: str | None = None
    # Where the source language's file goes when it does not follow the
    # template, like Android's default resources in values/.
    source_path: str | None = None
    # Language code -> the code this target's file names use, e.g. {"zh": "zh-Hans"}.
    codes: dict = field(default_factory=dict)
    name: str | None = None
    # What `path` is relative to: the config file's directory, or where the
    # command ran when --path came from the command line.
    root: Path = field(default_factory=Path.cwd)
    # What `source_path` is relative to, when that differs from `root`.
    source_root: Path | None = None

    @property
    def layout(self) -> str:
        return layout_of(self.format, self.path)

    @cached_property
    def template(self) -> PathTemplate:
        return PathTemplate(self.path, self.root)

    @property
    def location(self) -> Path:
        """The directory or file ``path`` names. Not meaningful for a template."""
        return self.root / self.path

    @property
    def source_file(self) -> Path | None:
        if not self.source_path:
            return None
        return (self.source_root or self.root) / self.source_path


@dataclass
class Config:
    api_key: str | None = None
    api_url: str = DEFAULT_API_URL
    project: int | None = None
    targets: list = field(default_factory=lambda: [Target()])
    # Whether the file listed "targets", rather than describing one set of
    # files at its top level.
    has_targets: bool = False
    # Where the file came from, for error messages that can point at it.
    source: Path | None = None

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                f'No API key. Set {ENV_API_KEY} in your environment, or pass --api-key.\n'
                f'Create a key under Developer access in the dashboard.'
            )
        return self.api_key

    def require_project(self) -> int:
        if self.project is None:
            raise ConfigError(
                f'No project. Add "project" to {CONFIG_FILENAME}, set {ENV_PROJECT}, '
                f'or pass --project.\nRun `localizeme init` to write a config file.'
            )
        return self.project


def find_config_file(start: Path | None = None) -> Path | None:
    """Nearest localizeme.json, searching upward from ``start``.

    Walking up means the command works from any subdirectory of a project, the
    way git and every other project-scoped tool behaves.
    """
    directory = (start or Path.cwd()).resolve()
    for candidate in [directory, *directory.parents]:
        path = candidate / CONFIG_FILENAME
        if path.is_file():
            return path
    return None


def read_config_file(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ConfigError(f'{path} is not valid JSON: {exc}') from exc
    except OSError as exc:
        raise ConfigError(f'Could not read {path}: {exc}') from exc

    if not isinstance(raw, dict):
        raise ConfigError(f'{path} should hold a JSON object, not a {type(raw).__name__}.')

    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ConfigError(
            f'{path} has unrecognised setting(s): {", ".join(unknown)}. '
            f'Known settings: {", ".join(sorted(KNOWN_KEYS))}.'
        )
    return raw


def _as_project_id(value, where: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f'{where} should be a project id (a number), got {value!r}.') from None


def check_target(target: Target, where: str) -> None:
    """Refuse a target that cannot work, before anything is fetched or written."""
    if target.format not in FORMAT_EXTENSIONS:
        raise ConfigError(
            f'{where}: unknown format {target.format!r}. '
            f'Choose one of: {", ".join(sorted(FORMAT_EXTENSIONS))}.'
        )
    try:
        template = target.template
        source = PathTemplate(target.source_path, target.root) if target.source_path else None
    except TemplateError as exc:
        raise ConfigError(f'{where}: {exc}') from None

    if template.is_template and target.format in MULTI_LANGUAGE_FORMATS:
        raise ConfigError(
            f'{where}: {target.format} keeps every language in one file, so its path '
            f'cannot contain a placeholder. Name the file, e.g. "Localizable.xcstrings".'
        )
    if target.source_path is not None:
        if not template.is_template:
            raise ConfigError(
                f'{where}: "source_path" goes with a path that has {PLACEHOLDER_NAMES} in it.'
            )
        if source.is_template:
            raise ConfigError(
                f'{where}: "source_path" names the one file the source language goes to, '
                f'so it cannot contain a placeholder.'
            )
    if target.codes and not template.is_template:
        raise ConfigError(
            f'{where}: "codes" renames languages in a path that has {PLACEHOLDER_NAMES} in it, '
            f'and this path has none.'
        )


def _codes(value, where: str) -> dict:
    valid = isinstance(value, dict) and all(
        isinstance(key, str) and key and isinstance(code, str) and code
        for key, code in value.items()
    )
    if not valid:
        raise ConfigError(
            f'"codes" in {where} should map a language code to the code its files use, '
            f'like {{"zh": "zh-Hans"}}.'
        )
    return dict(value)


def check_single_file_language(target: Target) -> None:
    """A path naming one file holds one language, so it needs to know which."""
    if (
        target.layout == 'file'
        and target.format not in MULTI_LANGUAGE_FORMATS
        and not target.language
    ):
        raise ConfigError(
            f'{target.path} names one file, but {target.format} holds one language per '
            f'file. Put {{code}} in the path, or give it a language ("language" in '
            f'{CONFIG_FILENAME}, or --language).'
        )


def _target(data: dict, root: Path, where: str) -> Target:
    settings = {
        key: str(data[key])
        for key in ('name', 'format', 'path', 'platform', 'language', 'source_path')
        if data.get(key) is not None
    }
    if data.get('codes') is not None:
        settings['codes'] = _codes(data['codes'], where)
    target = Target(root=root, **settings)
    check_target(target, where)
    return target


def _targets_from(data: dict, path: Path) -> tuple[list[Target], bool]:
    root = path.parent
    if 'targets' not in data:
        return [_target(data, root, str(path))], False

    stray = sorted(set(data) & TARGET_KEYS)
    if stray:
        raise ConfigError(
            f'{path} has "targets" and also {", ".join(stray)} at the top level. '
            f'Each target carries its own, so move them into the targets they belong to.'
        )
    items = data['targets']
    if not isinstance(items, list) or not items:
        raise ConfigError(
            f'"targets" in {path} should be a list, each entry with a "format" and a "path".'
        )

    targets = []
    for number, item in enumerate(items, start=1):
        where = f'target {number} in {path}'
        if not isinstance(item, dict):
            raise ConfigError(f'{where} should be an object, not a {type(item).__name__}.')
        unknown = sorted(set(item) - TARGET_KEYS)
        if unknown:
            raise ConfigError(
                f'{where} has unrecognised setting(s): {", ".join(unknown)}. '
                f'Known settings: {", ".join(sorted(TARGET_KEYS))}.'
            )
        missing = [f'"{key}"' for key in ('format', 'path') if not item.get(key)]
        if missing:
            raise ConfigError(f'{where} needs {" and ".join(missing)}.')
        targets.append(_target(item, root, where))

    names = [target.name for target in targets if target.name]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ConfigError(f'{path} has more than one target named {", ".join(repeated)}.')
    return targets, True


def load_config(args, start: Path | None = None) -> Config:
    """Resolve settings from flags, environment and the nearest config file.

    Flags that describe one target (--format, --path, ...) are applied later by
    ``select_targets``, which knows which targets a command is working on.
    """
    config = Config()

    path = find_config_file(start)
    if path is not None:
        config.source = path
        data = read_config_file(path)
        if 'project' in data:
            config.project = _as_project_id(data['project'], f'"project" in {path}')
        if data.get('api_url') is not None:
            config.api_url = str(data['api_url'])
        config.targets, config.has_targets = _targets_from(data, path)
    else:
        config.targets = [Target(root=Path.cwd())]

    if os.environ.get(ENV_API_URL):
        config.api_url = os.environ[ENV_API_URL]
    if os.environ.get(ENV_PROJECT):
        config.project = _as_project_id(os.environ[ENV_PROJECT], ENV_PROJECT)
    config.api_key = os.environ.get(ENV_API_KEY) or None

    # Flags win. Only override when actually supplied, or a flag's default would
    # quietly beat the config file it is meant to complement.
    for name in ('api_url', 'api_key'):
        value = getattr(args, name, None)
        if value is not None:
            setattr(config, name, value)
    if getattr(args, 'project', None) is not None:
        config.project = _as_project_id(args.project, '--project')

    config.api_url = config.api_url.rstrip('/')
    return config


def select_targets(config: Config, args) -> list[Target]:
    """The targets a pull or push works on, with flags applied to them.

    --target picks targets by name. --format, --path, --platform, --source-path
    and --file describe a single target, so they need exactly one in play;
    applying one to several would, say, write an iOS catalog's values into an
    Android file. --language applies to every target.
    """
    targets = list(config.targets)
    where = str(config.source) if config.source else 'the configuration'

    wanted = getattr(args, 'target', None) or []
    if wanted:
        named = [target.name for target in targets if target.name]
        unknown = [name for name in wanted if name not in named]
        if unknown and named:
            raise ConfigError(
                f'No target named {", ".join(unknown)}. {where} has: {", ".join(named)}.'
            )
        if unknown:
            raise ConfigError(
                f'--target picks a target by its "name", and {where} names none. '
                f'Give each target a "name".'
            )
        targets = [target for target in targets if target.name in wanted]

    given = [
        (name, flag, getattr(args, name, None))
        for name, flag in TARGET_FLAGS
        if getattr(args, name, None) is not None
    ]
    single = [flag for _, flag, _ in given]
    if getattr(args, 'file', None):
        single.append('--file')
    if single and len(targets) > 1:
        raise ConfigError(
            f'{", ".join(single)} applies to one target, and {where} has {len(targets)}. '
            f'Pick one with --target.'
        )

    language = getattr(args, 'language', None)
    selected = []
    for target in targets:
        changes = {name: value for name, _, value in given}
        # A path typed on the command line is relative to where it was typed,
        # and one from the file stays relative to the file.
        if 'source_path' in changes:
            changes['source_root'] = Path.cwd()
        elif 'path' in changes:
            changes['source_root'] = target.source_root or target.root
        if 'path' in changes:
            changes['root'] = Path.cwd()
            # source_path and codes only mean something beside a template, and
            # no flag can unset them, so a --path that is a directory or one
            # file leaves them behind rather than refusing to run.
            file_format = changes.get('format', target.format)
            if layout_of(file_format, changes['path']) != 'template':
                changes.setdefault('source_path', None)
                changes['codes'] = {}
        if language is not None:
            changes['language'] = language
        target = replace(target, **changes)
        check_target(target, 'the command line' if given else where)
        check_single_file_language(target)
        selected.append(target)
    return selected

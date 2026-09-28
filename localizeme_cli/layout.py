"""Which file holds which language.

A target's ``path`` decides how its files are laid out, and takes one of three
shapes:

* a **template** such as ``locales/{code}.json`` or
  ``app/src/main/res/values-{android_code}/strings.xml`` holds one file per
  language, each named by filling the placeholders in;
* **one file**, a path ending in the format's own extension such as
  ``ios/App/Localizable.xcstrings``, is a catalog that carries every language,
  or a target pinned to a single language;
* a **directory** such as ``locales`` holds files that keep the names the API
  gives them. This was the only shape before templates and still works
  unchanged.

Templates are what native projects need. Android keeps each language in its own
resource directory and iOS in its own .lproj, and neither matches a flat export
name like ``strings_de.xml``, so without templates every pull needed a rename
script around it.
"""

import glob
import re
from pathlib import Path, PurePath

# The extension each format is written with, so a pull into a directory can name
# its file and `init` can suggest something sensible.
FORMAT_EXTENSIONS = {
    'json': '.json',
    'po': '.po',
    'ios-strings': '.strings',
    'ios-xcstrings': '.xcstrings',
    'android-xml': '.xml',
    'csv': '.csv',
    'yaml': '.yaml',
    'xliff': '.xlf',
    'arb': '.arb',
    'properties': '.properties',
}

# Every extension a file of each format may carry. The CLI only ever writes the
# first; the others are what people name them by hand, and `push` should find
# a messages.yml as readily as a messages.yaml.
FORMAT_SUFFIXES = {name: (extension,) for name, extension in FORMAT_EXTENSIONS.items()} | {
    'yaml': ('.yaml', '.yml'),
    'xliff': ('.xlf', '.xliff'),
}

# A single .xcstrings holds every language, so it is never one file per language.
MULTI_LANGUAGE_FORMATS = {'ios-xcstrings'}


class TemplateError(ValueError):
    """A path template cannot be used. The message is meant for a human."""


def android_qualifier(code: str) -> str:
    """The resource-directory qualifier Android uses for a language code.

    ``de`` stays ``de``, and a region takes Android's ``r`` form, so ``pt-BR``
    becomes ``pt-rBR``. Anything more, such as a script (``zh-Hans``) or a
    numeric region (``es-419``), needs the BCP 47 form ``b+zh+Hans``, which
    Android reads from API 24.
    """
    parts = [part for part in re.split(r'[-_]', code) if part]
    if not parts:
        return code
    language, rest = parts[0].lower(), parts[1:]
    plain = 2 <= len(language) <= 3 and language.isalpha()
    if plain and not rest:
        return language
    if plain and len(rest) == 1 and len(rest[0]) == 2 and rest[0].isalpha():
        return f'{language}-r{rest[0].upper()}'
    return '+'.join(['b', language, *(_bcp47_subtag(part) for part in rest)])


def _bcp47_subtag(part: str) -> str:
    if len(part) == 4 and part.isalpha():
        return part.title()  # a script: Hans, Latn
    if len(part) == 2 and part.isalpha():
        return part.upper()  # a region: TW
    return part  # a numeric region such as 419, or a variant


# What each placeholder turns a language code into.
PLACEHOLDERS = {
    'code': lambda code: code,                      # de, pt-BR: as the project spells it
    'locale': lambda code: code.replace('-', '_'),  # pt_BR: gettext, Java, Flutter
    'android_code': android_qualifier,              # pt-rBR, b+zh+Hans
}

PLACEHOLDER_NAMES = ', '.join('{' + name + '}' for name in PLACEHOLDERS)

_PLACEHOLDER = re.compile(r'\{([^{}]*)\}')


def layout_of(file_format: str, path: str) -> str:
    """``'template'``, ``'file'`` or ``'directory'``. See the module docstring."""
    if _PLACEHOLDER.search(path):
        return 'template'
    if PurePath(path).suffix.lower() in FORMAT_SUFFIXES.get(file_format, ()):
        return 'file'
    return 'directory'


class PathTemplate:
    """A path with placeholders standing in for the language.

    Only ``raw``, the path as the user wrote it, is read for placeholders.
    ``root`` is what it is relative to, and a brace in the name of some
    directory above the project must not turn into a placeholder.
    """

    def __init__(self, raw: str, root: Path):
        self.raw = raw
        self.root = root
        self.placeholders = _PLACEHOLDER.findall(raw)
        unknown = sorted({name for name in self.placeholders if name not in PLACEHOLDERS})
        if unknown:
            shown = ', '.join('{' + name + '}' for name in unknown)
            raise TemplateError(
                f'{raw} uses {shown}, which is not a placeholder. '
                f'Use {PLACEHOLDER_NAMES}.'
            )
        if self.placeholders:
            # The fixed directory the template starts from, and the rest of it
            # as a glob to find candidates and a pattern to read them back.
            parts = PurePath(raw).parts
            first = next(i for i, part in enumerate(parts) if _PLACEHOLDER.search(part))
            self._base = root.joinpath(*parts[:first])
            self._glob = '/'.join(_glob_part(part) for part in parts[first:])
            self._regex = _template_regex(parts[first:])

    @property
    def is_template(self) -> bool:
        return bool(self.placeholders)

    def render(self, code: str) -> Path:
        """The file a language's strings go to."""

        def fill(match):
            value = PLACEHOLDERS[match.group(1)](code)
            # A code ends up as part of a path, so it must not be able to climb
            # out of the directory the template names.
            if value in ('', '.', '..') or re.search(r'[/\\\0]', value):
                raise TemplateError(f'Language code {code!r} cannot be used in a file path.')
            return value

        return self.root / _PLACEHOLDER.sub(fill, self.raw)

    def find(self) -> list[tuple[Path, dict]]:
        """Every file on disk the template describes, with what fills each placeholder."""
        if not self._base.is_dir():
            return []
        found = []
        for path in sorted(self._base.glob(self._glob)):
            if not path.is_file():
                continue
            match = self._regex.fullmatch(path.relative_to(self._base).as_posix())
            if match:
                found.append((path, match.groupdict()))
        return found

    def match(self, path: Path) -> dict | None:
        """What fills each placeholder, if ``path`` is one of the template's files."""
        try:
            relative = path.resolve().relative_to(self._base.resolve())
        except ValueError:
            return None
        match = self._regex.fullmatch(relative.as_posix())
        return match.groupdict() if match else None


def _alternating(pieces):
    # split() with one group alternates literal text and placeholder names.
    return ((index % 2 == 1, piece) for index, piece in enumerate(pieces))


def _glob_part(part: str) -> str:
    pieces = _PLACEHOLDER.split(part)
    return ''.join('*' if odd else glob.escape(piece) for odd, piece in _alternating(pieces))


def _template_regex(parts) -> re.Pattern:
    pieces = _PLACEHOLDER.split('/'.join(parts))
    seen = set()
    out = []
    for odd, piece in _alternating(pieces):
        if not odd:
            out.append(re.escape(piece))
        elif piece in seen:
            # The same placeholder twice must say the same thing twice.
            out.append(f'(?P={piece})')
        else:
            seen.add(piece)
            out.append(f'(?P<{piece}>[^/]+)')
    return re.compile(''.join(out))


class ProjectLanguages:
    """A project's languages, and every code each one answers to.

    A language has one code of its own and may answer to others (Norwegian is
    ``no`` and also ``nb``), so a file named for any of them belongs to it.
    Everything here maps back to the language's own code, since that is what
    the API is sent.
    """

    def __init__(self, rows, source: str | None = None):
        self.codes: list[str] = []
        self._aliases: dict[str, list[str]] = {}
        self._names: dict[str, str] = {}
        for row in rows or []:
            code = row.get('code')
            if not code:
                continue
            self.codes.append(code)
            if row.get('name'):
                self._names[code] = row['name']
            self._aliases[code] = [alt for alt in (row.get('alt_codes') or []) if alt]
            if source is None and row.get('is_source'):
                source = code
        self.source = source
        self._by_spelling: dict[str, str] = {}
        for spelling, code in self.spellings():
            self._by_spelling.setdefault(spelling.casefold(), code)

    @classmethod
    def from_project(cls, payload: dict) -> 'ProjectLanguages':
        source = (payload.get('source_language') or {}).get('code')
        return cls(payload.get('languages'), source)

    def spellings(self) -> list[tuple[str, str]]:
        """``(spelling, code)`` pairs: every language's own code, then its others.

        Own codes come first so that where one language's alias is another's
        code, the code wins.
        """
        pairs = [(code, code) for code in self.codes]
        pairs += [(alt, code) for code in self.codes for alt in self._aliases[code]]
        return pairs

    def resolve(self, spelling) -> str | None:
        """The language a code names, by any of its spellings, or None."""
        return self._by_spelling.get(str(spelling).casefold())

    def describe(self, code: str) -> str:
        """``Norwegian (no)``: a bare code can read as a word in a sentence."""
        name = self._names.get(code)
        return f'{name} ({code})' if name else code


_NAME_SEPARATORS = re.compile(r'([_.\-])')

# How a region or script is written inside a language tag: DE, GB, 419, Hans.
_REGION = re.compile(r'[A-Z]{2}|[0-9]{3}|[A-Z][a-z]{3}')
# How a language is: de, fil.
_LANGUAGE_WORD = re.compile(r'[a-z]{2,3}')


def _extends_tag(word: str) -> bool:
    """Whether the word right after a code reads as its region or script.

    Case is not trusted here, since web projects write pt-br as often as pt-BR.
    """
    return (len(word) == 2 and word.isalpha()) or bool(_REGION.fullmatch(word))


def _resolve_run(languages: ProjectLanguages, run: str) -> str | None:
    # Java and gettext write pt_BR where the project may say pt-BR, and the
    # other way round.
    for spelling in (run, run.replace('_', '-'), run.replace('-', '_')):
        code = languages.resolve(spelling)
        if code is not None:
            return code
    return None


def language_in_name(name, languages: ProjectLanguages) -> str | None:
    """The language a file's name says it holds, or None.

    Reads the names export writes (``translations_de.json``,
    ``strings_pt-BR.xml``), a bare ``de.json``, names that lead with the code
    (``de_messages.json``) and Java's ``messages_pt_BR.properties``. A code
    only counts as the whole name, its end or its start, and only whole:

    * ``strings_en-GB.json`` in a project with only ``en`` is no language at
      all, not English with ``GB`` left over. Guessing would put British
      strings into ``en``, and a push cannot take that back.
    * A region is not a language: ``DE`` in ``messages_de_DE`` is Germany, not
      German, and ``IN`` in ``strings_en_IN`` is India, not Indonesian.

    The whole name beats its end, which beats its start, since export puts the
    code last; after that a longer code wins.
    """
    pieces = _NAME_SEPARATORS.split(PurePath(name).stem)
    words, separators = pieces[0::2], pieces[1::2]
    last = len(words) - 1
    best, best_rank = None, None
    for start in range(len(words)):
        run = ''
        for end in range(start, len(words)):
            run = run + separators[end - 1] + words[end] if end > start else words[start]
            at_start, at_end = start == 0, end == last
            if not (at_start or at_end):
                continue  # a code in the middle of a name is part of something else
            if not at_end and _extends_tag(words[end + 1]):
                continue  # en in en-GB
            if not at_start and _REGION.fullmatch(words[start]) \
                    and _LANGUAGE_WORD.fullmatch(words[start - 1]):
                continue  # DE in de_DE
            code = _resolve_run(languages, run) if run else None
            if code is None:
                continue
            rank = (at_start and at_end, at_end, at_start, len(run))
            if best_rank is None or rank > best_rank:
                best, best_rank = code, rank
    return best

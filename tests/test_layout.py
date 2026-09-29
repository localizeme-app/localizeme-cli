"""The rules for which file holds which language.

These are pure functions over names and a temporary directory, so they are
tested directly. test_cli.py covers the same rules end to end, through a real
HTTP server.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from localizeme_cli.layout import (
    PathTemplate,
    ProjectLanguages,
    TemplateError,
    android_qualifier,
    language_in_name,
    layout_of,
)

LANGUAGES = ProjectLanguages([
    {'code': 'en', 'is_source': True},
    {'code': 'de'},
    {'code': 'pt'},
    {'code': 'pt-BR'},
    {'code': 'no', 'alt_codes': ['nb', 'nb-NO']},
    {'code': 'he', 'alt_codes': ['iw']},
])


class AndroidQualifierTests(unittest.TestCase):
    def test_a_bare_language_is_used_as_it_is(self):
        self.assertEqual(android_qualifier('de'), 'de')
        self.assertEqual(android_qualifier('fil'), 'fil')

    def test_a_region_takes_androids_r_form(self):
        self.assertEqual(android_qualifier('pt-BR'), 'pt-rBR')
        self.assertEqual(android_qualifier('pt_br'), 'pt-rBR')

    def test_scripts_and_numeric_regions_need_the_bcp47_form(self):
        """values-zh-rHans would be read as a region called "Hans" and ignored."""
        self.assertEqual(android_qualifier('zh-Hans'), 'b+zh+Hans')
        self.assertEqual(android_qualifier('zh-hant-tw'), 'b+zh+Hant+TW')
        self.assertEqual(android_qualifier('es-419'), 'b+es+419')
        self.assertEqual(android_qualifier('sr-Latn'), 'b+sr+Latn')


class LanguageInNameTests(unittest.TestCase):
    def test_the_names_export_writes(self):
        self.assertEqual(language_in_name('translations_de.json', LANGUAGES), 'de')
        self.assertEqual(language_in_name('messages_de.properties', LANGUAGES), 'de')
        self.assertEqual(language_in_name('Localizable.de.strings', LANGUAGES), 'de')

    def test_a_regional_code_is_matched_whole(self):
        """Splitting pt-BR into pt and BR used to lose it altogether."""
        self.assertEqual(language_in_name('strings_pt-BR.xml', LANGUAGES), 'pt-BR')
        self.assertEqual(language_in_name('pt-BR.json', LANGUAGES), 'pt-BR')
        self.assertEqual(language_in_name('strings_pt.xml', LANGUAGES), 'pt')

    def test_another_spelling_finds_the_language_it_belongs_to(self):
        self.assertEqual(language_in_name('nb.json', LANGUAGES), 'no')
        self.assertEqual(language_in_name('translations_nb-NO.json', LANGUAGES), 'no')

    def test_a_code_at_the_end_beats_one_at_the_start(self):
        self.assertEqual(language_in_name('de_messages.json', LANGUAGES), 'de')
        self.assertEqual(language_in_name('en_de.json', LANGUAGES), 'de')

    def test_a_region_the_project_lacks_is_no_language(self):
        """Not English with GB left over: that would put British strings into en."""
        for name in ('strings_en-GB.json', 'en-GB.json', 'en_us.json', 'Localizable.en-AU.strings'):
            with self.subTest(name=name):
                self.assertIsNone(language_in_name(name, LANGUAGES))
        spanish = ProjectLanguages([{'code': 'es'}])
        self.assertIsNone(language_in_name('es-419.json', spanish))

    def test_a_region_is_not_read_as_a_language(self):
        """DE is Germany, not German; IN is India, not Indonesian's old code."""
        german = ProjectLanguages([{'code': 'de'}])
        self.assertIsNone(language_in_name('messages_de_DE.properties', german))
        indonesian = ProjectLanguages([{'code': 'en'}, {'code': 'id', 'alt_codes': ['in']}])
        self.assertIsNone(language_in_name('strings_en_IN.xml', indonesian))

    def test_a_code_in_the_middle_of_a_name_is_not_read(self):
        self.assertIsNone(language_in_name('strings_de_old.json', ProjectLanguages([{'code': 'de'}])))

    def test_underscores_and_hyphens_spell_the_same_code(self):
        """Java names messages_pt_BR.properties where the project says pt-BR."""
        self.assertEqual(language_in_name('messages_pt_BR.properties', LANGUAGES), 'pt-BR')
        self.assertEqual(language_in_name('app_de.arb', LANGUAGES), 'de')

    def test_no_code_is_none(self):
        self.assertIsNone(language_in_name('strings.json', LANGUAGES))
        self.assertIsNone(language_in_name('Localizable.xcstrings', LANGUAGES))

    def test_a_code_must_be_a_whole_word(self):
        """"messages" ends in "es", but that is not Spanish."""
        spanish = ProjectLanguages([{'code': 'es'}])
        self.assertIsNone(language_in_name('messages.json', spanish))


class LayoutTests(unittest.TestCase):
    def test_a_placeholder_makes_a_template(self):
        self.assertEqual(layout_of('json', 'locales/{code}.json'), 'template')

    def test_the_formats_own_extension_makes_one_file(self):
        self.assertEqual(layout_of('ios-xcstrings', 'App/Localizable.xcstrings'), 'file')
        self.assertEqual(layout_of('yaml', 'config/de.yml'), 'file')

    def test_anything_else_is_a_directory(self):
        self.assertEqual(layout_of('json', 'locales'), 'directory')
        self.assertEqual(layout_of('json', 'my.app/locales'), 'directory')


class PathTemplateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def touch(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x')
        return path

    def test_an_unknown_placeholder_is_named(self):
        with self.assertRaises(TemplateError) as caught:
            PathTemplate('locales/{lang}.json', self.root)
        self.assertIn('{lang}', str(caught.exception))
        self.assertIn('{code}', str(caught.exception))

    def test_render_fills_every_placeholder(self):
        template = PathTemplate('res/values-{android_code}/strings.xml', self.root)
        self.assertEqual(template.render('pt-BR'), self.root / 'res/values-pt-rBR/strings.xml')
        template = PathTemplate('{code}/app_{locale}.arb', self.root)
        self.assertEqual(template.render('pt-BR'), self.root / 'pt-BR/app_pt_BR.arb')

    def test_a_code_cannot_climb_out_of_the_template(self):
        template = PathTemplate('locales/{code}/strings.json', self.root)
        for code in ('..', '.', 'a/b'):
            with self.subTest(code=code), self.assertRaises(TemplateError):
                template.render(code)

    def test_find_reads_each_placeholder_back(self):
        self.touch('res/values/strings.xml')
        self.touch('res/values-de/strings.xml')
        self.touch('res/values-pt-rBR/strings.xml')
        self.touch('res/values-de/colors.xml')
        template = PathTemplate('res/values-{android_code}/strings.xml', self.root)
        found = {path.relative_to(self.root).as_posix(): values for path, values in template.find()}
        self.assertEqual(found, {
            'res/values-de/strings.xml': {'android_code': 'de'},
            'res/values-pt-rBR/strings.xml': {'android_code': 'pt-rBR'},
        })

    def test_find_with_nothing_there_is_empty(self):
        template = PathTemplate('missing/{code}.json', self.root)
        self.assertEqual(template.find(), [])

    def test_a_repeated_placeholder_must_agree_with_itself(self):
        self.touch('de/de.json')
        self.touch('de/fr.json')
        template = PathTemplate('{code}/{code}.json', self.root)
        found = [path.relative_to(self.root).as_posix() for path, _ in template.find()]
        self.assertEqual(found, ['de/de.json'])

    def test_match_tells_a_templates_file_from_any_other(self):
        template = PathTemplate('ios/{code}.lproj/Localizable.strings', self.root)
        self.assertEqual(
            template.match(self.root / 'ios/de.lproj/Localizable.strings'), {'code': 'de'},
        )
        self.assertIsNone(template.match(self.root / 'ios/de.lproj/Other.strings'))
        self.assertIsNone(template.match(self.root / 'elsewhere/de.lproj/Localizable.strings'))

    def test_glob_characters_around_a_placeholder_are_literal(self):
        """Unescaped, [old].json is a character class that matches o.json."""
        self.touch('de/[old].json')
        self.touch('de/o.json')
        template = PathTemplate('{code}/[old].json', self.root)
        found = [path.relative_to(self.root).as_posix() for path, _ in template.find()]
        self.assertEqual(found, ['de/[old].json'])


class ProjectLanguagesTests(unittest.TestCase):
    def test_the_source_comes_from_the_project_or_the_flagged_row(self):
        self.assertEqual(LANGUAGES.source, 'en')
        payload = {'source_language': {'code': 'de'}, 'languages': [{'code': 'de'}]}
        self.assertEqual(ProjectLanguages.from_project(payload).source, 'de')
        self.assertIsNone(ProjectLanguages([{'code': 'de'}]).source)

    def test_any_spelling_resolves_to_the_languages_own_code(self):
        self.assertEqual(LANGUAGES.resolve('NB'), 'no')
        self.assertEqual(LANGUAGES.resolve('iw'), 'he')
        self.assertIsNone(LANGUAGES.resolve('fr'))


if __name__ == '__main__':
    unittest.main()

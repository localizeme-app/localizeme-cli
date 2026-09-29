# LocalizeMe CLI

Pull and push [LocalizeMe](https://localizeme.app/?src=github-cli) translations from your build or CI,
as the files your app compiles and where your project keeps them: Android
`values-de/strings.xml`, iOS `Localizable.xcstrings` or `de.lproj`, Flutter
`app_de.arb`, web `locales/de.json`.

No runtime dependencies — everything it needs is in the Python standard library,
so a CI step that installs it cannot break because of somebody else's release.
Python 3.10 or newer.

```bash
pipx install localizeme
```

## Setup

Create an API key under **Developer access** in the dashboard. A read-only key,
which is what the dashboard makes unless you switch it, is all `pull` and
`init` need; `push` needs one with full access. Then:

```bash
export LOCALIZEME_API_KEY=lz_live_...
localizeme init --list                      # which projects can this key see?
localizeme init --project 42 --format xliff --path locales
```

`init` writes a `localizeme.json` you commit with the project:

```json
{
  "project": 42,
  "format": "xliff",
  "path": "locales"
}
```

The API key is deliberately *not* in it. Settings resolve **flag → environment →
`localizeme.json` → default**, so the file holds the boring facts and the
credential stays in the environment. Paths in the file are relative to the file
itself, so `localizeme pull` from a subdirectory writes where the file says.

| Variable | Purpose |
| --- | --- |
| `LOCALIZEME_API_KEY` | Your `lz_` key. Required. |
| `LOCALIZEME_API_URL` | Override the API base, e.g. to point at sandbox. |
| `LOCALIZEME_PROJECT` | Project id, if you would rather not commit one. |

## Where files go

`path` takes one of three shapes:

| `path` | Layout |
| --- | --- |
| `locales` | A directory. Files keep the names the API gives them: `translations_de.json`, `strings_de.xml`. |
| `src/locales/{code}.json` | A template. One file per language, named by filling in the placeholder. |
| `ios/App/Localizable.xcstrings` | One file: a catalog that carries every language, or a target pinned to one `language`. |

A template can use any of these placeholders, each a different spelling of the
same language code:

| Placeholder | `de` | `pt-BR` | `zh-Hans` | For |
| --- | --- | --- | --- | --- |
| `{code}` | `de` | `pt-BR` | `zh-Hans` | Web frameworks, iOS `.lproj` directories |
| `{locale}` | `de` | `pt_BR` | `zh_Hans` | gettext, Java `.properties`, Flutter `.arb` |
| `{android_code}` | `de` | `pt-rBR` | `b+zh+Hans` | Android resource directories |

An Android app keeps its default strings in `values/`, not `values-en/`, so a
template takes a `source_path` for the project's source language:

```json
{
  "project": 42,
  "format": "android-xml",
  "platform": "android",
  "path": "app/src/main/res/values-{android_code}/strings.xml",
  "source_path": "app/src/main/res/values/strings.xml"
}
```

The same with `init`:

```bash
localizeme init --project 42 --format android-xml --platform android \
  --path 'app/src/main/res/values-{android_code}/strings.xml' \
  --source-path app/src/main/res/values/strings.xml
```

`codes` gives a language a different code in this target's file names, when
the platform wants one other than the project's. `{"codes": {"zh": "zh-Hans"}}`
writes `zh-Hans.lproj` where `{code}.lproj` would have written `zh.lproj`.

### Several apps in one repo

`targets` lists several sets of files, each with its own format, layout and
platform. A repo with an Android app and an iOS app:

```json
{
  "project": 42,
  "targets": [
    {
      "name": "android",
      "format": "android-xml",
      "platform": "android",
      "path": "app/src/main/res/values-{android_code}/strings.xml",
      "source_path": "app/src/main/res/values/strings.xml"
    },
    {
      "name": "ios",
      "format": "ios-xcstrings",
      "platform": "ios",
      "path": "ios/App/Localizable.xcstrings"
    }
  ]
}
```

Each target pulls its own platform's values, so a string with an iOS-specific
value reaches the iOS app and the Android app gets its own. `pull` and `push`
work on every target; `--target ios` picks one by its `name`. `--format`,
`--path`, `--platform`, `--source-path` and `--file` describe a single target,
so with several they need `--target` too. `--language` applies to all of them.

## Commands

```bash
localizeme pull                     # every language, every target
localizeme pull --language de       # just one
localizeme pull --target ios        # just one target
localizeme pull --group Checkout    # just one feature's keys

localizeme push --dry-run           # what would change, writing nothing
localizeme push --overwrite         # replace existing values
```

With a template, `push` finds the files the template describes and reads each
one's language from its path, so `values-pt-rBR/strings.xml` is Brazilian
Portuguese. A directory that is not a language, like `values-night/`, is
skipped with a note. With `source_path`, the source language is pushed from
that file alone, so a `values-en/` holding a few English-only strings beside
`values/` is left out. `--language` pushes just that language's file.

With a directory, `push` reads each file's language from its name, so
`translations_de.xlf`, `messages_pt_BR.properties`, `strings_pt-BR.xml` and a
plain `de.json` all resolve. It checks each one against the languages the
project actually has, and a name for a region the project lacks is not guessed
into the base language: `strings_en-GB.xml` in a project with only `en` fails
with a message rather than putting British strings into `en`. Here
`--language` overrides the name for every file. `--file` pushes specific paths
instead of scanning.

An `.xcstrings` catalog carries every language, so it is pushed as it is, with
no language to work out.

`pull` writes nothing until every target's export has arrived, so a failure
part way through leaves your files as they were rather than half from each
pull. `push` works out every target's files before it sends any.

`push` writes to the target's platform. A value that matches the shared one is
left as it is, and one that differs becomes that platform's own value, so
editing a string in the Android file changes it for Android only.

`--group` narrows a pull to the keys in a key group, by name. It is repeatable
and a key in any of the named groups is pulled, so `--group Checkout --group
Errors` gets both. `--group none` pulls the keys that are in no group. The
files are still whole documents of their format, just covering fewer keys —
which is what makes it useful for shipping one feature's strings on their own.

A language can answer to more than one code, so a file named for a second
spelling lands on the language it belongs to rather than making a new one:
`no.json` and `nb.json` both reach Norwegian, and so do `values-no/` and
`values-nb/`. If both exist with different strings, `push` stops for that
language rather than guess; identical copies are pushed once. `pull
--language` takes any of the codes too, and names the file after the code you
asked for, which is how one set of strings ships as `no` on Android and `nb`
on iOS.

### Formats

`json`, `po`, `ios-strings`, `ios-xcstrings`, `android-xml`, `csv`, `yaml`,
`xliff` (XLIFF 1.2), `arb` (Flutter), `properties` (Java) — the same set the
dashboard offers. `push` also finds `.yml` and `.xliff` files.

## Exit codes

The exit code is the real interface. A CI step reads it, not the prose.

| Code | Meaning |
| --- | --- |
| `0` | Fine. |
| `1` | The command ran, but some of the work in it failed. |
| `2` | The command was used or configured wrongly. |
| `3` | The API could not be reached, or refused the request. |

**`1` and `3` are kept apart on purpose.** A build that fails because the network
blipped must not look like a build that failed because a file in it was bad.

## In CI

`pull` is a step inside a build you already have, not a workflow of its own. The
files it writes are worth nothing on their own — they matter to whatever compiles
or bundles them next, so put it before that:

```yaml
      # in an existing job, ahead of the step that builds your app
      - run: pipx install localizeme
      - run: localizeme pull
        env:
          LOCALIZEME_API_KEY: ${{ secrets.LOCALIZEME_API_KEY }}
```

## About LocalizeMe

[LocalizeMe](https://localizeme.app/?src=github-cli) is a localization platform
for product teams: every string your apps ship, in every language, in one
place.
Translators work in an editor with review statuses, screenshots that show
where a string appears, key groups and the full history of every change.

- **Unlimited keys and languages on every plan, the free one included.** Paid
  plans are one flat price per workspace, not per seat or per string.
- **The files every platform uses:** Android `strings.xml`, iOS `.xcstrings`
  and `.strings`, Flutter ARB, JSON, YAML, CSV, XLIFF 1.2, gettext PO and Java
  `.properties`, with a separate value per platform where iOS and Android need
  to differ.
- **Strings over the air:** the
  [iOS](https://github.com/localizeme-app/localizeme-ios-sdk) and
  [Android](https://github.com/localizeme-app/localizeme-android-sdk) SDKs
  update an app's text without a store release.
- **For developers and their agents:** this CLI
  for builds and CI, a REST API, and an MCP server that Claude, Cursor and
  other coding agents can work with.

[Start free](https://localizeme.app/register?src=github-cli), no credit card needed ·
[Pricing](https://localizeme.app/pricing?src=github-cli) ·
[Developers](https://localizeme.app/developers?src=github-cli)

## License

Source-available under the [PolyForm Shield License 1.0.0](LICENSE), the same
terms as the LocalizeMe mobile SDKs. Use it in any build or pipeline,
commercial or not, and read and change it as you need. What it does not allow
is using the CLI, or anything made from it, to provide a product that competes
with the CLI or with LocalizeMe. Keep the `LICENSE` file, or its link and the
`Required Notice` line, with any copy you pass on.

## Development

```bash
pip install -e .
PYTHONPATH=. python -m unittest discover -s tests
```

Tests run a real `HTTPServer` on a loopback port rather than stubbing `urllib`,
so the multipart encoding, auth header, zip handling and exit codes are all
exercised the way a shell exercises them.

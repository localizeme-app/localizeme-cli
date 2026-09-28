# Releasing

The CLI is developed in the LocalizeMe monorepo, under `cli/`. This repository
is an export of that directory, and a release is a tag here: the
`Publish to PyPI` workflow tests, builds and publishes the tagged commit as
`localizeme`. The tag has to match `__version__` in
`localizeme_cli/__init__.py`.

1. If the release relies on an API change, check that change is in production
   first. An older API refuses what the new CLI sends, and a build that pulls
   in the release fails with it.
2. In the monorepo, set `__version__` in `cli/localizeme_cli/__init__.py` and
   merge it.
3. Export `cli/` from that commit into a clone of this repository. Removing
   everything first means a file deleted in the monorepo goes here too:

   ```bash
   cd localizeme-cli
   git rm -rq --ignore-unmatch .
   git -C ../localizeme archive <monorepo commit>:cli | tar -x
   git add -A
   git commit -m "Export cli/ from localizeme <monorepo commit>"
   git push origin main
   ```

4. Tag and push: `git tag -a 0.1.0 -m 0.1.0 && git push origin 0.1.0`. Tags are
   plain versions with no `v` prefix, the same as the SDKs.
5. Once the workflow is green and the version is on
   https://pypi.org/project/localizeme/, mark the release on GitHub:
   `gh release create 0.1.0 --verify-tag`, with `--prerelease` for a beta.

PyPI never takes the same version twice, even after a delete, so a release that
went wrong is fixed with the next version rather than a re-upload.

## One-time PyPI setup

Nothing publishes until PyPI trusts the workflow. There is no token to create
or store.

1. Create an account at https://pypi.org and turn on two-factor authentication,
   which PyPI requires for anything beyond browsing.
2. Under Account → Publishing, add a pending GitHub publisher:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `localizeme` |
   | Owner | `localizeme-app` |
   | Repository name | `localizeme-cli` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. Release as above. A pending publisher does not reserve the name, so the
   project only exists, and is only yours, once the first release is uploaded.

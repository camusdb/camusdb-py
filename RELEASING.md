# CI and releases

## CI

GitHub Actions runs `.github/workflows/ci.yml` on each push to `main` and on each pull request:

| Job | What it does |
| --- | --- |
| Lint & type check | `ruff check`, `ruff format --check` and `mypy`. |
| Unit tests | `pytest` on Python 3.10 to 3.14. |
| Integration tests | The integration tests against `camusdb/camusdb:latest` in Docker, on Python 3.10 and 3.14. |
| Integration tests (authentication) | The authentication tests against the same image with `CAMUSDB_AUTH_ENABLED=true`. The job skips them, with a notice, when the image has no authentication. |
| Build distributions | `uv build`, `twine check`, a test install of the wheel, and the `dist` artifact. |

Dependabot (`.github/dependabot.yml`) proposes updates of the actions and of `uv.lock` each week.

## Releases

`.github/workflows/release.yml` publishes a version. It runs when you push a tag that starts with
`v`. It runs all of CI first, and it then publishes the wheel and the source archive that CI built.
Thus a push to `main` never publishes anything.

The package is published on PyPI as `camusdb-py`. The module that users import is `camusdb`.

To release:

1. Set the same version in `pyproject.toml` and in `camusdb.__version__`. A unit test makes sure
   that they agree.
2. Commit, then push a tag with that version: `git tag v0.2.0 && git push origin v0.2.0`.
3. The workflow refuses a tag that does not match the version in `pyproject.toml`. It publishes to
   PyPI, and then creates a GitHub release with the files and generated notes.

PyPI refuses a version that it has already. If a release fails before the upload, fix the cause and
run the failed jobs again. If the fix needs a new commit, move the tag to that commit. After a
successful upload, release a new version instead.

## One-time setup

These steps are done for `camusdb/camusdb-py`. Do them again only for a new repository or a new
PyPI project:

1. On PyPI, add a trusted publisher for the project `camusdb-py`: owner `camusdb`, repository
   `camusdb-py`, workflow `release.yml`, environment `pypi`. For a project that is not on PyPI yet,
   add it as a pending publisher. The project name must be the `name` in `pyproject.toml`. No API
   token is stored in GitHub.
2. In the GitHub repository settings, create the environment `pypi`. To require an approval before
   each upload, add required reviewers to it.

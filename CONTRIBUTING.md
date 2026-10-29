# Development

The project uses [uv](https://docs.astral.sh/uv/). Install the environment, then run the unit tests
and the checks:

```shell
uv sync
uv run pytest                     # unit tests; the integration tests are skipped
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

CI runs the same checks on each pull request. See [RELEASING.md](RELEASING.md) for the CI jobs.

## Integration tests

The integration tests need a CamusDB server. Each run creates its own database and drops it at the
end. To start a server with Docker:

```shell
docker run -d -p 5095:5095 --name camusdb camusdb/camusdb:latest
CAMUSDB_TEST_ENDPOINT=http://localhost:5095 uv run pytest
```

The authentication tests need a server with `CAMUSDB_AUTH_ENABLED=true` and a superuser:

```shell
CAMUSDB_TEST_AUTH_ENDPOINT=http://localhost:5095 CAMUSDB_TEST_AUTH_USER=admin \
CAMUSDB_TEST_AUTH_PASSWORD=... uv run pytest tests/test_integration_auth.py
```

Things to know about the authentication tests:

- **The server refuses credentials over plain HTTP, except from loopback** (`CADB0519`). Under Docker
  port mapping, the server does not see the client as loopback. On Linux, start the container with
  `--network host`, as CI does. On macOS, `--network host` does not help, because Docker runs the
  containers in a VM. For a local server only, start it with `--require-tls-when-auth-enabled false`.
- **The bootstrap superuser is created some seconds after the server answers `/ping`.** A login
  before that fails with `CADB0516`.
- **The server permits 20 logins for each account each minute.** Two runs within one minute can fail
  with `CADB0518`. Wait one minute, then run again.

`test_backups` runs only when the server has a backup directory (`kahuna.backup_dir`) and accepts the
backup calls. With authentication off, the server accepts backup calls from loopback only, so under
Docker port mapping the test is skipped, as in CI.

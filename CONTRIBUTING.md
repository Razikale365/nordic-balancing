# Contributing

## Setup

```bash
uv sync
```

## Checks

Run the same gates as CI before opening a pull request:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Live tests

`uv run pytest -m live` calls the real APIs and is not run by default. Keys go in a local
`.env` copied from `.env.example`; that file lists `FINGRID_API_KEY` and `ENTSOE_API_KEY`.
Never commit `.env`.

## Changes

- Describe a behaviour change in [docs/API_CONTRACT.md](docs/API_CONTRACT.md) first, then
  change the code.
- Every bug fix adds a regression test that fails on the previous release and passes with the
  fix.
- Offline tests use `httpx.MockTransport` and are not presented as live verification.
- Keep pull requests small and focused.
- Add a line to [CHANGELOG.md](CHANGELOG.md).

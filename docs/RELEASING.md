# Releasing to PyPI

Publishing uses PyPI trusted publishing (OIDC) from `.github/workflows/release.yml`.
No API token is stored in GitHub or locally.

## One-time setup (done once, by the PyPI account owner)

1. Log in at https://pypi.org, then open **Account settings → Publishing → Add a new
   pending publisher → GitHub**, and enter:
   - PyPI project name: `nordic-balancing`
   - Owner: `Razikale365`
   - Repository name: `nordic-balancing`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. GitHub already has the `pypi` environment. It requires a reviewer's approval before
   publishing, and only accepts tags matching `v*`.

## Each release

1. Set `version` in `pyproject.toml` (for example `0.1.0`), run `uv lock`, and run the
   gates:
   `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q && uv run pytest -q -m live`
2. Commit and push to `main`, then wait for CI to pass.
3. Create a GitHub release with the tag `v<version>` (for example `v0.1.0`) targeting
   `main`.
4. The Release workflow runs the gates, checks that the tag matches the version, builds,
   and runs `twine check`. It then waits for approval of the `pypi` environment. Approve
   it under **Actions → the run → Review deployments**.
5. Check https://pypi.org/p/nordic-balancing, then `pip install nordic-balancing==<version>`
   in a clean environment.

The first publish turns the pending publisher into a normal trusted publisher for the
project.

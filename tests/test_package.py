import re
import tomllib
from pathlib import Path

import nordic_balancing

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_exposed() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert nordic_balancing.__version__ == project["version"]


def test_httpx_is_the_only_required_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {re.split(r"[<>=!~;\[ ]", dep, maxsplit=1)[0] for dep in project["dependencies"]}
    assert names == {"httpx"}


def test_python_floor_is_3_12_and_ci_tests_every_classified_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12"
    classified = sorted(
        c.rsplit(" :: ", 1)[1]
        for c in project["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    )
    assert classified[0] == "3.12"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    check_job = re.search(r"^  check:.*?(?=^  \S)", ci, re.DOTALL | re.MULTILINE)
    assert check_job is not None
    check_block = check_job.group(0)
    matrix = re.search(r"matrix:\s+python-version: \[(.*?)\]", check_block)
    assert matrix is not None
    assert sorted(v.strip().strip('"') for v in matrix.group(1).split(",")) == classified
    assert "python-version: ${{ matrix.python-version }}" in check_block
    assert "- run: uv run pytest" in check_block


def test_lowest_direct_installs_the_declared_pandas_floor() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    floor = ci.split("  lowest-direct:", 1)[1]
    assert '"pandas==2.1.1"' in floor
    assert '"pandas==2.1.*"' not in floor
    assert "first 2.1.x release with Python 3.12 wheels" in floor
    assert "2.1.0 needs a source build" in floor


def test_live_workflow_scopes_the_api_keys_to_the_two_steps_that_read_them() -> None:
    live = (ROOT / ".github" / "workflows" / "live.yml").read_text(encoding="utf-8")
    assert not re.search(r"^env:", live, re.MULTILINE)
    header, steps = live.split("    steps:", 1)
    assert "    env:" not in header
    for key in ("FINGRID_API_KEY", "ENTSOE_API_KEY"):
        assert steps.count(key + ": ${{ secrets." + key + " }}") == 2
    for step in re.split(r"^      - ", steps, flags=re.MULTILINE)[1:]:
        reads_keys = step.startswith(("name: Require both API keys", "name: Run the live tests"))
        for key in ("FINGRID_API_KEY", "ENTSOE_API_KEY"):
            assert ("          " + key + ": ${{ secrets." + key + " }}" in step) == reads_keys


def test_release_checks_the_dists_with_a_pinned_twine() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "- run: uvx twine@6.1.0 check dist/*" in release
    assert "uvx twine check" not in release

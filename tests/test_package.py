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
    matrix = re.search(r"matrix:\s+python-version: \[(.*?)\]", ci)
    assert matrix is not None
    assert sorted(v.strip().strip('"') for v in matrix.group(1).split(",")) == classified
    check_job = re.search(r"^  check:.*?(?=^  \S)", ci, re.DOTALL | re.MULTILINE)
    assert check_job is not None
    assert "- run: uv run pytest" in check_job.group(0)

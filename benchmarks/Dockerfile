# Reproducible benchmark environment for nordic-balancing.
#
# Build from the repository root:
#   docker build -f benchmarks/Dockerfile -t nb-perf .
#
# Run with fixed resources and the fixtures mounted read-only (fixtures are
# never committed or redistributed; see benchmarks/README.md):
#   docker run --rm --cpus=2 --memory=4g ^
#     -v <fixtures dir>:/fixtures:ro ^
#     -v %cd%\benchmarks\results:/work/benchmarks/results ^
#     -e NB_FIXTURES=/fixtures -e NB_DOCKER_IMAGE=nb-perf ^
#     nb-perf --sets entsoe-imb-SE3-year-2025 --tag docker-baseline
#
# --cpus/--memory make the compared runs share effective cgroup limits; the
# container records them from /sys/fs/cgroup in each result's environment block.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /work
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
COPY src ./src
COPY benchmarks ./benchmarks
RUN uv sync --frozen --no-dev --group benchmarks
ENTRYPOINT ["uv", "run", "--frozen", "--no-sync", "--no-dev", "--group", "benchmarks", "python", "benchmarks/run.py"]

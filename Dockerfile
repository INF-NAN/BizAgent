# App image: API + gateway (and, with a different command, the env-manager).
# Build from a checkout with submodules initialized: git submodule update --init
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy PYTHONPYCACHEPREFIX=/tmp/pycache PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY third_party/agent-world-model third_party/agent-world-model
COPY src src
RUN uv sync --frozen --no-dev
COPY configs configs
COPY ui ui
COPY tests/fixtures tests/fixtures
EXPOSE 8080 8090
CMD ["uv", "run", "--no-sync", "workbench", "api", "serve"]

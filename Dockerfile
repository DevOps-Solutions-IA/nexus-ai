# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 AS uv
FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN ln -sf /usr/bin/python /opt/venv/bin/python && ln -sf /usr/bin/python /opt/venv/bin/python3 && ln -sf /usr/bin/python /opt/venv/bin/python3.14

FROM cgr.dev/chainguard/python:latest@sha256:1f37785e5cdb70151f36aaa15e1e3cef4571424dbefbf4b0d8a9222535cb13ff
WORKDIR /app
COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv
COPY --from=builder --chown=65532:65532 /build/src/nexus_ai /app/nexus_ai
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
ENTRYPOINT ["/opt/venv/bin/uvicorn", "nexus_ai.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--no-access-log"]

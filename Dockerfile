# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv
FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
RUN ln -sf /usr/bin/python /opt/venv/bin/python && ln -sf /usr/bin/python /opt/venv/bin/python3 && ln -sf /usr/bin/python /opt/venv/bin/python3.14

FROM cgr.dev/chainguard/python:latest@sha256:1f37785e5cdb70151f36aaa15e1e3cef4571424dbefbf4b0d8a9222535cb13ff
WORKDIR /app
COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv
COPY --from=builder --chown=65532:65532 /build/src/nexus_ai /app/nexus_ai
COPY --from=builder --chown=65532:65532 /build/migrations /app/migrations
COPY --from=builder --chown=65532:65532 /build/alembic.ini /app/alembic.ini
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 NXS_ENVIRONMENT=production
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["/opt/venv/bin/uvicorn", "nexus_ai.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--no-access-log", "--no-server-header"]

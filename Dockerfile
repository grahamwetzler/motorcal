# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.27 AS uv

FROM python:3.13-slim AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime
RUN groupadd --gid 1000 motorcal && useradd --uid 1000 --gid motorcal --create-home motorcal \
    && mkdir /state && chown motorcal:motorcal /state
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml ./
COPY --chown=motorcal:motorcal data /data
ENV PATH="/app/.venv/bin:$PATH"
USER motorcal
EXPOSE 8000
ENTRYPOINT ["motorcal"]
CMD ["serve", "--config", "/data", "--state", "/state/state.yaml"]

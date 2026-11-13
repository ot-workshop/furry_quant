ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG PGCLIENT_IMAGE=postgres:18.6-bookworm
FROM ${PGCLIENT_IMAGE} AS pgclients
FROM ${NODE_IMAGE} AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS runtime
ARG APP_UID=1000
ARG APP_GID=1000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    TZ=Asia/Shanghai UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONPATH=/app/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates tzdata libpq5 liblz4-1 libzstd1 libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.10.0 \
    && groupadd --gid ${APP_GID} quant && useradd --uid ${APP_UID} --gid quant --create-home quant
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN test -f /app/src/finance_quant/data/__init__.py \
    && test -f /app/src/finance_quant/application/__init__.py \
    && python -c "from pathlib import Path; import site; Path(site.getsitepackages()[0], 'finance_quant-source.pth').write_text('/app/src\\n', encoding='utf-8')"
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY --from=pgclients /usr/lib/postgresql/18/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=pgclients /usr/lib/postgresql/18/bin/pg_restore /usr/local/bin/pg_restore
COPY --from=pgclients /usr/lib/*-linux-gnu/libpq.so.5* /usr/local/lib/
RUN ldconfig && pg_dump --version && pg_restore --version
RUN mkdir -p /app/storage && chown quant:quant /app/storage
COPY --from=frontend /build/dist ./frontend/dist/
USER quant
EXPOSE 8000
CMD ["python", "-m", "finance_quant.server", "api"]

FROM runtime AS test
USER root
COPY tests/ ./tests/
RUN uv sync --frozen --extra dev --no-install-project
USER quant
CMD ["python", "-m", "pytest", "-q", "--basetemp", "/tmp/finance-quant-tests", "-o", "cache_dir=/tmp/finance-quant-pytest-cache"]

FROM runtime AS application

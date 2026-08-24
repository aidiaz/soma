FROM python:3.14-slim

WORKDIR /app

# garminconnect requires >=3.12, so this base cannot drop to 3.11.
RUN pip install --no-cache-dir uv

# Dependencies first: they change far less often than the source, so this layer
# survives most rebuilds. Important on a Pi, where a cold build is slow.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

COPY scripts/ ./scripts/

# The database and the Garmin token store live here. compose mounts a named
# volume over it; creating it keeps the image runnable without one.
RUN mkdir -p /app/data

# Put uv's venv on PATH so the console scripts resolve bare, everywhere: the
# compose command, `docker compose run`, and `docker exec`. Without this only
# `uv run <script>` works, and a bare `garmin-sync` in a compose command fails
# with "not found" — which that loop's `|| true` then hides for a whole deploy.
ENV PATH="/app/.venv/bin:$PATH" \
    TRAINDB_DB_PATH=/app/data/traindb.db \
    TRAINDB_GARMIN_TOKENSTORE=/app/data/garmin_tokens \
    TRAINDB_HOST=0.0.0.0 \
    TRAINDB_PORT=8000

EXPOSE 8000

# Default to the HTTP server. Each ingestion worker is the same image with a
# different command (see compose.pi.yaml), which keeps them in lockstep — a
# mapper change can never be deployed to one and not the other.
CMD ["traindb-http"]

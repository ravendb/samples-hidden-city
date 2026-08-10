# Pinned to match PYTHON_IMAGE_TAG in versions.env -- preflight asserts the two
# agree. Bump both together, never just one.
FROM python:3.13.14-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY data/ data/

# uv==0.12.2 must match UV_VERSION in versions.env (preflight asserts this).
# --frozen: install exactly what uv.lock pins, fail instead of re-resolving --
# `pip install .` here previously re-resolved every dependency's latest
# compatible version on every build despite uv.lock existing in the repo.
RUN pip install --no-cache-dir uv==0.12.2 && \
    uv sync --frozen --no-dev

# So bare `python`/`uvicorn` (as used by the CMD below and by the scraper/worker
# k8s manifests' `command: ["python", "-m", ...]`) resolve to uv's venv instead
# of needing every call site rewritten to `uv run ...`.
ENV PATH="/app/.venv/bin:$PATH"

# Default entrypoint — overridden per-component in k8s manifests.
# agent:  uvicorn src.agent.app:app --host 0.0.0.0 --port 8001
# scraper: python -m src.scraper.run
# worker:  python -m src.worker.run
CMD ["uvicorn", "src.agent.app:app", "--host", "0.0.0.0", "--port", "8001"]

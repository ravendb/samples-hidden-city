FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/
COPY data/ data/
COPY scripts/ scripts/

# Default entrypoint — overridden per-component in k8s manifests
CMD ["uvicorn", "src.agent.app:app", "--host", "0.0.0.0", "--port", "8000"]

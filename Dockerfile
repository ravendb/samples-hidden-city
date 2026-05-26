FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/
COPY data/ data/

# Default entrypoint — overridden per-component in k8s manifests.
# agent:  uvicorn src.agent.app:app --host 0.0.0.0 --port 8000
# scraper: python -m src.scraper.run
# worker:  python -m src.worker.run
CMD ["uvicorn", "src.agent.app:app", "--host", "0.0.0.0", "--port", "8000"]

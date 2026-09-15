FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY knowledge_runtime ./knowledge_runtime
RUN pip install --no-cache-dir -e ".[runtime]"

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "knowledge_runtime.v2.cli", "v2", "status"]


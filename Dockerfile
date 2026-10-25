FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RESOLVE_DB_PATH=/data/resolve.db
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home resolve && mkdir /data && chown resolve /data

USER resolve
EXPOSE 8000
CMD ["uvicorn", "resolve.api:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[service]"

# non-root
RUN useradd -r -u 10001 -g root app && chown -R app /app
USER 10001

EXPOSE 8000
# Web pod (default). The CronJob overrides command with: python -m timesheet.run --email
CMD ["uvicorn", "timesheet.service.main:app", "--host", "0.0.0.0", "--port", "8000"]

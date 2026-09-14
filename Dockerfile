# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[service]"

# non-root
RUN useradd -r -u 10001 -g root app && chown -R app /app
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
# The web service. The scheduled refresh overrides this with:
#   python -m timesheet.run --send
CMD ["uvicorn", "timesheet.service.main:app", "--host", "0.0.0.0", "--port", "8000"]

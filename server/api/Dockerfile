FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The slim image ships the standard library without bytecode, and
# PYTHONDONTWRITEBYTECODE keeps it that way at run time, so every short-lived
# python here - the compose healthcheck, `docker compose run detect` -
# compiled urllib, http.client, email, ... from source on each start: 0.5-0.9
# CPU-s per healthcheck on the e2-micro, 4-5 CPU-s and a 5 s timeout while the
# box was busy (measured 2026-09-29, docs/performance.md).
RUN python -m compileall -q -j 0 /usr/local/lib/python3.13

COPY app ./app

RUN useradd --system --no-create-home --uid 10001 api
USER api

EXPOSE 8000

# One worker: the target box has one core. Caddy sits in front, hence the
# proxy-header flags.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

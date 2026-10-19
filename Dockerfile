# PassOrch - simulation-only audit-planning platform.
#
# Runs the real application from the source tree via the supported entry
# point (``python -m uvicorn app.api.app:app``).  The frontend is served
# by the application itself; there is no separate build step.  No engine
# or password-cracking tool is included or executed - by design.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime dependency bounds live in pyproject.toml; constraints.txt pins
# the verified closure.  pip installs the project (and its dependencies)
# from there.  The source tree stays in place because the frontend is
# resolved relative to the package files.
COPY pyproject.toml constraints.txt ./
COPY app ./app
COPY frontend ./frontend

RUN pip install --no-cache-dir -c constraints.txt . \
    && useradd --create-home --uid 1000 passorch \
    && mkdir -p /data \
    && chown passorch:passorch /data

# Persistence defaults to the mounted volume; override via environment.
# No secret (such as PASSORCH_API_TOKEN) is ever set here.
ENV PASSORCH_JOB_DB=/data/passorch.db

USER passorch

EXPOSE 8000

# /health is intentionally unauthenticated, side-effect free, and does
# not touch the database, discovery, or any engine.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

# 0.0.0.0 is required so the port is reachable through the container's
# network namespace; publish it on loopback only (see README/Compose).
CMD ["python", "-m", "uvicorn", "app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

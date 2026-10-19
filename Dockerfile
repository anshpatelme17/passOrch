# PassOrch - simulation-only audit-planning platform.
#
# Runs the real application from the source tree via the supported entry
# point (``python -m uvicorn app.api.app:app``).  The frontend is served
# by the application itself; there is no separate build step.  No engine
# or password-cracking tool is included or executed - by design.

# Base image pinned by the multi-arch index digest (resolved via
# ``docker buildx imagetools inspect python:3.13-slim``): tags are
# mutable, digests are not.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies install from the hash-verified lock (every artifact hash
# from the PyPI JSON API); the project itself installs with --no-deps so
# pip never re-resolves anything unlocked.  The source tree stays in
# place because the frontend is resolved relative to the package files.
# Note: pip's isolated build environment fetches setuptools (build
# backend) unhashed - documented in THREAT_MODEL.md.
COPY pyproject.toml requirements.lock ./
COPY app ./app
COPY frontend ./frontend

RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 1000 passorch \
    && mkdir -p /data \
    && chown passorch:passorch /data

# Persistence defaults to the mounted volume; override via environment.
# No secret (such as PASSORCH_API_TOKEN) is ever set here.
ENV PASSORCH_JOB_DB=/data/passorch.db

USER passorch

EXPOSE 8000

# Readiness, not just liveness: /health/ready is unauthenticated,
# read-only (in-memory store listings, no SQLite access, no writes) and
# answers 503 with a generic not_ready detail if either store fails, so
# the orchestrator stops routing to an instance whose stores are broken.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2).read()"]

# 0.0.0.0 is required so the port is reachable through the container's
# network namespace; publish it on loopback only (see README/Compose).
CMD ["python", "-m", "uvicorn", "app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

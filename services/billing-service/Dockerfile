# syntax=docker/dockerfile:1
# =============================================================================
# billing-service — hardened production image
# Multi-stage · python:3.12-slim · non-root · runtime deps only · read-only-friendly
# (CIS-aligned: minimal surface, unprivileged user, no build tools in final image)
# =============================================================================

# ── Builder: install runtime deps into a venv ───────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install runtime + the infra extras the deploy VALUES select (postgres, redis).
# An image that lacks the driver its own values default to boots straight into ConfigError;
# safety:image-runtime gates exactly that. dev/ci stay out for a lean, low-CVE image.
COPY pyproject.toml README.md ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[pdf,postgres,redis,s3,vault]"

# ── Final: slim runtime, non-root ───────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Create an unprivileged user; never run as root (CWE-250 / least privilege).
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENV=production \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app pyproject.toml README.md ./

USER app
EXPOSE 8000

# Liveness (K8s/Compose can also probe /readyz).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"

# Read-only-friendly: the app writes nothing outside a mounted volume (object
# store) and /tmp. Run with `--read-only --tmpfs /tmp` for extra hardening.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

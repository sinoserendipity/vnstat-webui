# ─── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Only copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Install samba client for pysmb (runtime dependency)
RUN apt-get update && apt-get install -y --no-install-recommends \
    smbclient \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r vnstat && useradd -r -g vnstat -d /app -s /sbin/nologin vnstat

# Copy application code
COPY app.py .
COPY templates/ templates/

# Create writable directories for temp cache, persistent settings, and gunicorn control socket
RUN mkdir -p /tmp/vnstat /app/data && \
    chown -R vnstat:vnstat /tmp/vnstat /app/data /app

USER vnstat

EXPOSE 5050

# Use gunicorn as production WSGI server
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--threads", "2", "--timeout", "30", "--worker-tmp-dir", "/tmp", "app:app"]

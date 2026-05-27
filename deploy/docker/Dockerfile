# syntax=docker/dockerfile:1.7
# Lean image for AWS EC2 / any Linux host.
# Two stages: builder (compiles deps) and runtime (slim).

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt pyproject.toml ./
COPY src ./src

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip wheel && \
    pip install -r requirements.txt && \
    pip install -e .

# ----- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    AUTOMATION_HOST=0.0.0.0 \
    AUTOMATION_PORT=8080

# Playwright runtime deps (chromium). Comment out if you do not use a browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        tini curl && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash automation
WORKDIR /opt/automation

COPY --from=builder /opt/venv /opt/venv
COPY --chown=automation:automation src /opt/automation/src
COPY --chown=automation:automation config /opt/automation/config
COPY --chown=automation:automation plugins_external /opt/automation/plugins_external
COPY --chown=automation:automation pyproject.toml requirements.txt /opt/automation/

# Install playwright browsers as the runtime user
RUN mkdir -p /opt/automation/data/{logs,state,profiles,screenshots,learning,downloads} && \
    chown -R automation:automation /opt/automation
USER automation
RUN playwright install chromium || true

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "automation", "server", "--config", "/opt/automation/config/config.json", "--host", "0.0.0.0", "--port", "8080"]

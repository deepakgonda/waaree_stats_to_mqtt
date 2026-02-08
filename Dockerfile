# ── Stage 1: Install dependencies & Playwright browser ──────────
FROM python:3.11-slim-bookworm AS builder

# Prevent interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive

# Install Playwright system-level dependencies (Chromium needs these)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright/Chromium deps
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    # fonts for proper page rendering
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install only Chromium (smallest footprint)
RUN playwright install --with-deps chromium

# ── Stage 2: Final slim image ───────────────────────────────────
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Copy system packages installed for Playwright/Chromium
COPY --from=builder /usr/lib /usr/lib
COPY --from=builder /usr/share/fonts /usr/share/fonts
COPY --from=builder /etc/fonts /etc/fonts

# Copy installed Python packages
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy Playwright browsers (installed to /root by default)
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

WORKDIR /app
COPY poller.py .

# Non-root would be nice but Playwright's Chromium on ARM needs
# --no-sandbox which is already set in the script's launch args.

CMD ["python", "poller.py"]

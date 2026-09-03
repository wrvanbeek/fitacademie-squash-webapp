# FitAcademie Squash Webapp — Dockerfile (single-stage for Playwright compat)
FROM python:3.12-slim-bookworm

WORKDIR /app

# System deps (minimal for Playwright chromium)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Copy project
COPY scripts/ ./scripts/
COPY frontend/ ./frontend/

# Install Python deps + Playwright + Chromium
RUN pip install --no-cache-dir \
    fastapi uvicorn sqlalchemy aiosqlite pydantic pydantic-settings \
    python-jose bcrypt cryptography apscheduler python-multipart \
    httpx email-validator jinja2 python-dotenv playwright \
    && playwright install chromium 2>&1 | tail -5 \
    && playwright install-deps chromium 2>&1 | tail -5 || true \
    && rm -rf /root/.cache/pip

# Environment
ENV HOST=0.0.0.0
ENV PORT=8080
ENV DATABASE_URL=sqlite+aiosqlite:///./data/app.db
ENV JWT_SECRET=flyio-fa-squash-jwt-2026
ENV ENCRYPTION_KEY=LTJdEKgid0sTiWTnT5RG54n_Z7XDfV6J1w8gxIVUnzs=
ENV FITACADEMIE_BASE_URL=https://portaal.fitacademie.nl/club_portal/lessons
ENV SCHEDULER_TIMEZONE=Europe/Amsterdam
ENV RELOAD=false

# Fly.io volume mount for SQLite persistence
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8080

CMD ["python", "-u", "scripts/main.py"]
# FitAcademie Squash Webapp — Lightweight Dockerfile (pure-Python, no Playwright)
FROM python:3.12-slim-bookworm

WORKDIR /app

# Copy project
COPY scripts/ ./scripts/
COPY frontend/ ./frontend/

# Install minimal Python deps (no Playwright!)
RUN pip install --no-cache-dir \
    fastapi uvicorn sqlalchemy aiosqlite pydantic pydantic-settings \
    python-jose bcrypt cryptography pydantic-email-validation \
    python-multipart email-validator python-dotenv \
    requests beautifulsoup4 \
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

# Volume for SQLite persistence
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8080

CMD ["python", "-u", "scripts/main.py"]
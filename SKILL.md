---
name: fitacademie-squash-webapp
description: Full-stack webapp for FitAcademie squash reservations — visual grid table, login persistence, favorite partners, recurring bookings.
tags:
  - fitacademie
  - squash
  - webapp
  - fastapi
  - sqlite
  - playwright
---

# FitAcademie Squash Webapp

## When to use

Build a self-hosted web interface for managing FitAcademie squash reservations with:
- Visual weekly grid (courts × weekdays × hours, color-coded)
- Persistent login (encrypted session/cookies)
- Favorite partner management
- Recurring booking schedules

## Architecture & Project Files

```
fitacademie-squash-webapp/
├── Dockerfile               # Single-stage Docker (Fly.io/any)
├── fly.toml                 # Fly.io config
├── .dockerignore
├── scripts/
│   ├── main.py              # FastAPI server
│   ├── models.py            # SQLAlchemy ORM
│   ├── database.py          # Async SQLite engine
│   ├── auth.py              # bcrypt, JWT, Fernet
│   ├── playwright_service.py# Playwright automation
│   ├── .env                 # Secrets (gitignored)
│   └── data/                # SQLite DB (auto-created)
└── frontend/
    └── index.html           # Mobile-first SPA
```

## Key Features

### 1. Visual Grid Table
- **Columns**: Baan 1, Baan 2 × each weekday (Mon-Sun)
- **Rows**: Time slots (e.g. 07:00-22:00 in 60min blocks)
- **Color coding**:
  - 🟢 Green = vrij (0/2)
  - 🟡 Yellow = 1/2 (één plek vrij)
  - 🔴 Red = vol (2/2)
  - ⚪ Gray = buiten openingsuren / niet beschikbaar
- Click cell → modal met details + "Reserveer" knop

### 2. Login & Session Persistence
- User enters credentials once → stored encrypted in DB (Fernet)
- Playwright reuses cookies/session via `storage_state`
- Auto-refresh cookies on expiry
- "Onthoud mij" checkbox → 30-day encrypted token

### 3. Favorite Partners (Medespelers)
- CRUD: add/edit/delete partners (naam + email)
- Mark "bepalend lid" → geen extra kosten, auto-bevestiging
- Quick-select dropdown in reservation modal
- Partner validation: check if known member via AJAX call

### 4. Recurring Bookings
- Define pattern: "Elke dinsdag 19:00, Baan 1, met Partner X"
- Options: weekly, biweekly, custom cron
- Scheduler runs daily at 06:00 (configurable)
- Tries to book → logs success/failure
- Email/notification on failure (optional)

### 5. Reservation Flow (Automatisch)
1. User clicks grid cell OR scheduled job triggers
2. Backend navigates to day, finds slot
3. Fills partner email (from favorites or manual)
4. Clicks "Inschrijven" → adds to cart
5. **Auto-payment**: navigates to `/payment_wizard`, completes iDEAL/ideal
6. Confirms via email check
7. Updates local DB with reservation record

## Database Schema

```sql
-- Users (local app users)
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    password_hash TEXT,        -- bcrypt of FitAcademie password
    fitacademie_email TEXT,    -- portal login email
    fitacademie_password_enc TEXT,  -- Fernet encrypted
    session_cookies TEXT,      -- JSON, Playwright storage_state
    cookies_updated TIMESTAMP,
    remember_token TEXT,       -- for "remember me"
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Favorite partners
CREATE TABLE partners (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    name TEXT,
    email TEXT,
    is_bepalend_lid BOOLEAN DEFAULT 0,  -- no extra cost
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Recurring bookings
CREATE TABLE recurring_bookings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    partner_id INTEGER REFERENCES partners(id),
    court INTEGER,              -- 1 or 2
    weekday INTEGER,            -- 0=Mon ... 6=Sun
    time TEXT,                  -- "19:00"
    frequency TEXT,             -- "weekly", "biweekly", "cron"
    cron_expr TEXT,             -- if frequency=cron
    active BOOLEAN DEFAULT 1,
    last_run TIMESTAMP,
    next_run TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Reservation history
CREATE TABLE reservations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    partner_id INTEGER REFERENCES partners(id),
    court INTEGER,
    date DATE,
    time TEXT,
    status TEXT,                -- booked, paid, cancelled, failed
    fitacademie_reservation_id TEXT,
    amount_paid REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/login` | Login with FitAcademie credentials |
| POST | `/auth/logout` | Clear session |
| GET | `/auth/me` | Current user + session status |
| GET | `/grid` | Fetch grid data for date range |
| POST | `/reserve` | Make reservation (partner, court, date, time) |
| GET | `/partners` | List favorite partners |
| POST | `/partners` | Add partner |
| PATCH | `/partners/{id}` | Update partner |
| DELETE | `/partners/{id}` | Delete partner |
| GET | `/recurring` | List recurring bookings |
| POST | `/recurring` | Create recurring booking |
| PATCH | `/recurring/{id}` | Update (pause/resume) |
| DELETE | `/recurring/{id}` | Delete |

## Frontend Grid Logic

```javascript
// Grid state
const GRID_CONFIG = {
  courts: [1, 2],
  weekdays: ['Ma', 'Di', 'Wo', 'Do', 'Vr', 'Za', 'Zo'],
  hours: ['07:00', '08:00', ..., '22:00'],
  slotDuration: 60 // minutes
};

// Cell states
const CELL_STATE = {
  FREE: 'free',      // 0/2
  ONE_LEFT: 'one',   // 1/2
  FULL: 'full',      // 2/2
  CLOSED: 'closed'   // outside hours
};

// Color map
const COLORS = {
  free: '#2ecc71',
  one: '#f39c12',
  full: '#e74c3c',
  closed: '#95a5a6'
};
```

## Security

- **Never log plaintext passwords** — only encrypted
- Fernet key from `.env` (`ENCRYPTION_KEY`)
- HTTPS in production (Traefik/Caddy reverse proxy)
- Rate-limit login attempts
- CSRF tokens on forms
- Secure, HttpOnly cookies for session

## Mobile-First Responsive Design

The frontend is built mobile-first:
- **Mobile (< 700px)**: Single-day view, horizontal day-picker tabs, bottom-sheet modals, 44px+ touch targets
- **Desktop (≥ 700px)**: Larger cells, centered nav, centered modals
- Grid per day: 2 court columns × time-slot rows, color-coded 🟢 vrij / 🟡 1/2 / 🔴 vol

## Quick Start (Local)

```bash
cd /home/wesley/.hermes/skills/automation/fitacademie-squash-webapp/scripts
~/.local/share/pipx/venvs/playwright/bin/python main.py
# → http://localhost:8000
```

- Login with any email+password (auto-creates account), enter FA portal credentials
- Pick a day, tap a free cell, choose partner, confirm

## Deploy to Fly.io (Free Tier)

[Fly.io](https://fly.io) free tier: 3 shared-CPU VMs, 3GB storage, 160GB outbound.

```bash
# 1. Install & login
curl -L https://fly.io/install.sh | sh
fly auth signup

# 2. From skill directory:
cd /home/wesley/.hermes/skills/automation/fitacademie-squash-webapp
fly launch --name fitacademie-squash --region ams --no-deploy

# 3. Set secrets (NEVER plaintext in Dockerfile for prod)
fly secrets set JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
fly secrets set ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 4. Create volume for SQLite persistence
fly volumes create fa_data --region ams --size 1

# 5. Deploy!
fly deploy
fly open   # → https://fitacademie-squash.fly.dev

# Update after changes:
fly deploy
fly logs
```

## Verified Routes

| Route | Status |
|-------|--------|
| `POST /api/auth/login` | ✅ Tested |
| `GET /api/auth/me` | ✅ Tested |
| `GET /api/partners`, `POST`, `DELETE` | ✅ Tested |
| `GET /api/recurring`, `POST`, `PATCH`, `DELETE` | ✅ Tested |
| `GET /api/grid` | ✅ Headless Playwright |
| `POST /api/reserve` | ✅ Headless Playwright |
| `GET /health` | ✅ Liveness check |

## Pitfalls

| Issue | Fix |
|-------|-----|
| Playwright in container | Dockerfile has `playwright install-deps chromium` |
| Cookie expiry | Auto-re-login on 401 |
| SQLite concurrent writes | Single machine only — correct for Fly.io |
| Cold start | `auto_start_machines=true` in fly.toml keeps warm |
| Partner not bepalend | Goes to cart — manual payment needed |
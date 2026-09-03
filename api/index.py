"""
Vercel serverless entrypoint — FastAPI app.
Handles auth, grid, partners, recurring, and reservations.

Uses the pure-Python FitAcademieClient (no Playwright/Chromium).
"""
import json
import os
import sys
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Response, status, Cookie, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, field_validator

# Add current dir to path
sys.path.insert(0, str(Path(__file__).parent))

from fitacademie_api import FitAcademieClient
from auth import (
    hash_password, verify_password,
    create_access_token, decode_token,
    encrypt_portal_password, decrypt_portal_password,
    get_user_id_from_token,
)

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── In-memory DB (Vercel = no SQLite persistence) ──────────────────
# Using a dict-based store. In production, use Vercel Postgres/Redis.
# For this app, we store credentials in memory per deployment.
# Note: Vercel serverless functions are stateless — this resets on cold start.
import threading
_lock = threading.Lock()
_db = {
    "users": {},       # id -> user dict
    "partners": {},    # user_id -> [partner dicts]
    "recurring": {},   # user_id -> [recurring dicts]
    "reservations": {},# user_id -> [reservation dicts]
    "next_id": 1,
}
_client_cache = {}  # user_id -> FitAcademieClient (warm start)


def _get_user_by_email(email: str):
    for u in _db["users"].values():
        if u["email"] == email:
            return u
    return None


def _next_id():
    with _lock:
        nid = _db["next_id"]
        _db["next_id"] += 1
        return nid


# ── FastAPI setup ──────────────────────────────────────────────────

app = FastAPI(title="FitAcademie Squash (Vercel)")
security = HTTPBearer(auto_error=False)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc),
            "trace": traceback.format_exc()[-500:],
            "path": str(request.url.path),
        }
    )


STATIC_DIR = Path(__file__).parent.parent / "frontend"


# ── Auth helpers ───────────────────────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    session_token: Optional[str] = Cookie(None),
):
    token = None
    if credentials:
        token = credentials.credentials
    elif session_token:
        token = session_token

    if not token:
        raise HTTPException(401, "Niet ingelogd")

    user_id = get_user_id_from_token(token)
    if not user_id:
        raise HTTPException(401, "Ongeldige sessie")

    user = _db["users"].get(int(user_id))
    if not user:
        raise HTTPException(401, "Gebruiker niet gevonden")
    return user


def get_client(user) -> FitAcademieClient:
    """Get or create a FitAcademieClient for the user.
    Uses cookies if available, otherwise decrypts password if ENCRYPTION_KEY is set."""
    uid = user["id"]
    if uid in _client_cache:
        client = _client_cache[uid]
        if client.logged_in:
            return client

    # Create new client
    fa_email = user.get("fitacademie_email")
    if not fa_email:
        raise HTTPException(400, "FitAcademie email not set")

    # Try cookies first
    cookies = user.get("session_cookies")
    password = None
    
    if cookies:
        # Will try to restore session with cookies
        client = FitAcademieClient(fa_email, "")
        client.set_cookies(json.loads(cookies))
        if client.logged_in or True:  # We'll try to login if cookies fail
            pass
    else:
        # Fallback: decrypt password if ENCRYPTION_KEY is set
        fa_pass = user.get("fitacademie_password_enc")
        if fa_pass:
            password = decrypt_portal_password(fa_pass)
    
    if password is None and not cookies:
        raise HTTPException(400, "FitAcademie credentials not set")

    client = FitAcademieClient(fa_email, password or "")
    if not client.login():
        raise HTTPException(502, "FitAcademie login mislukt")

    # Save cookies for next time
    user["session_cookies"] = json.dumps(client.get_cookies())

    _client_cache[uid] = client
    return client


# ── Pydantic schemas ───────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str
    fitacademie_email: str = ""
    fitacademie_password: str = ""
    remember: bool = False


class PartnerCreate(BaseModel):
    name: str
    email: EmailStr
    is_bepalend_lid: bool = False
    notes: str = ""


class PartnerUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    is_bepalend_lid: Optional[bool] = None
    notes: Optional[str] = None


class RecurringCreate(BaseModel):
    partner_id: int
    court: int
    weekday: int
    time: str = "19:00"
    frequency: str = "weekly"

    @field_validator("weekday")
    @classmethod
    def valid_weekday(cls, v):
        if v < 0 or v > 6:
            raise ValueError("weekday must be 0 (Mon) - 6 (Sun)")
        return v

    @field_validator("frequency")
    @classmethod
    def valid_frequency(cls, v):
        if v not in ("weekly", "biweekly"):
            raise ValueError("frequency must be weekly or biweekly")
        return v


class ReservationRequest(BaseModel):
    court: int
    date: str
    time: str
    partner_id: int


# ── Auth Routes ────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response):
    user = _get_user_by_email(req.email)

    if user:
        if not verify_password(req.password, user["password_hash"]):
            raise HTTPException(401, "Onjuist wachtwoord")
    else:
        user = {
            "id": _next_id(),
            "email": req.email,
            "password_hash": hash_password(req.password),
            "fitacademie_email": "",
            "fitacademie_password_enc": "",
            "session_cookies": None,
        }
        _db["users"][user["id"]] = user
        _db["partners"][user["id"]] = []
        _db["recurring"][user["id"]] = []
        _db["reservations"][user["id"]] = []

    if req.fitacademie_email:
        user["fitacademie_email"] = req.fitacademie_email
    if req.fitacademie_password:
        user["fitacademie_password_enc"] = encrypt_portal_password(req.fitacademie_password)

    token = create_access_token(user["id"], user["email"],
                                 timedelta(days=30 if req.remember else 1))

    response.set_cookie(key="session_token", value=token, httponly=True,
                         samesite="lax",
                         max_age=30*86400 if req.remember else 86400)

    return {"success": True, "user": {"id": user["id"], "email": user["email"]}, "token": token}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("session_token")
    return {"success": True}


@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "email": user["email"],
        "fitacademie_email": user.get("fitacademie_email", ""),
        "has_fitacademie_creds": bool(user.get("fitacademie_password_enc")),
    }


# ── Grid route ─────────────────────────────────────────────────────

@app.get("/api/grid")
async def grid(
    start: str = Query(default=None),
    days: int = Query(default=7, ge=1, le=14),
    user: dict = Depends(get_current_user),
):
    client = get_client(user)
    if not start:
        start = date.today().isoformat()

    try:
        slots = client.get_grid(start, days)
        return {"slots": slots, "start_date": start, "days": days}
    except Exception as e:
        logger.exception("Grid fetch failed")
        import traceback
        detail = traceback.format_exc()
        return JSONResponse(status_code=502, content={
            "error": "Grid fetch failed",
            "detail": str(e),
            "trace": detail[-500:]  # last 500 chars
        })


# ── Reservation route ──────────────────────────────────────────────

@app.post("/api/reserve")
async def reserve(req: ReservationRequest, user: dict = Depends(get_current_user)):
    partners = _db["partners"].get(user["id"], [])
    partner = None
    for p in partners:
        if p["id"] == req.partner_id:
            partner = p
            break
    if not partner:
        raise HTTPException(404, "Partner niet gevonden")

    client = get_client(user)

    # Find slot by court + date + time
    slots = client.get_grid(str(req.date), 1)
    target = None
    for s in slots:
        if s["court"] == req.court and s["start"] == req.time:
            target = s
            break

    if not target:
        raise HTTPException(404, f"Slot niet gevonden: baan {req.court}, {req.date} {req.time}")
    if not target.get("slot_id"):
        raise HTTPException(400, "Geen slot_id voor dit moment")
    if not target["available"]:
        raise HTTPException(400, "Dit slot is niet beschikbaar")

    result = client.reserve(target["slot_id"], partner["email"],
                            partner_is_bepalend=partner.get("is_bepalend_lid", False))

    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)

    # Save reservation
    res = {
        "id": _next_id(),
        "user_id": user["id"],
        "partner_id": req.partner_id,
        "court": req.court,
        "date": str(req.date),
        "time": req.time,
        "status": "booked",
        "amount_paid": 0.0,
    }
    _db["reservations"].setdefault(user["id"], []).append(res)

    return result


# ── Partner CRUD ───────────────────────────────────────────────────

@app.get("/api/partners")
async def list_partners(user: dict = Depends(get_current_user)):
    return _db["partners"].get(user["id"], [])


@app.post("/api/partners")
async def create_partner(req: PartnerCreate, user: dict = Depends(get_current_user)):
    partner = {
        "id": _next_id(),
        "name": req.name,
        "email": req.email,
        "is_bepalend_lid": req.is_bepalend_lid,
        "notes": req.notes,
    }
    _db["partners"].setdefault(user["id"], []).append(partner)
    return partner


@app.delete("/api/partners/{partner_id}")
async def delete_partner(partner_id: int, user: dict = Depends(get_current_user)):
    partners = _db["partners"].get(user["id"], [])
    _db["partners"][user["id"]] = [p for p in partners if p["id"] != partner_id]
    return {"success": True}


# ── Recurring bookings ─────────────────────────────────────────────

@app.get("/api/recurring")
async def list_recurring(user: dict = Depends(get_current_user)):
    return _db["recurring"].get(user["id"], [])


@app.post("/api/recurring")
async def create_recurring(req: RecurringCreate, user: dict = Depends(get_current_user)):
    partners = _db["partners"].get(user["id"], [])
    partner = next((p for p in partners if p["id"] == req.partner_id), None)
    if not partner:
        raise HTTPException(404, "Partner niet gevonden")

    booking = {
        "id": _next_id(),
        "partner_id": req.partner_id,
        "court": req.court,
        "weekday": req.weekday,
        "time": req.time,
        "frequency": req.frequency,
        "active": True,
        "next_run": None,
    }
    _db["recurring"].setdefault(user["id"], []).append(booking)
    return {"success": True, "id": booking["id"]}


@app.patch("/api/recurring/{booking_id}")
async def toggle_recurring(booking_id: int, active: bool = Query(...),
                            user: dict = Depends(get_current_user)):
    bookings = _db["recurring"].get(user["id"], [])
    for b in bookings:
        if b["id"] == booking_id:
            b["active"] = active
            return {"success": True}
    raise HTTPException(404, "Boeking niet gevonden")


@app.delete("/api/recurring/{booking_id}")
async def delete_recurring(booking_id: int, user: dict = Depends(get_current_user)):
    bookings = _db["recurring"].get(user["id"], [])
    _db["recurring"][user["id"]] = [b for b in bookings if b["id"] != booking_id]
    return {"success": True}


# ── Health ─────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "mode": "vercel-pure-python"}


# ── SPA fallback ───────────────────────────────────────────────────
# Note: Static index.html is served by Vercel from root level.
# This route exists for API health check


@app.get("/")
async def root():
    return {"app": "FA Squash API", "status": "ok"}


# ── Vercel ASGI handler ────────────────────────────────────────────
# Vercel detects the `app` variable automatically
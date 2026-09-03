"""FastAPI backend for FitAcademie Squash Webapp."""
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Load .env from same directory
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, Depends, HTTPException, Request, Response, status, Cookie, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Project root
sys.path.insert(0, str(Path(__file__).parent))
from database import init_db, get_db, AsyncSessionLocal
from models import User, Partner, RecurringBooking, Reservation
from auth import (
    hash_password, verify_password,
    create_access_token, decode_token,
    encrypt_portal_password, decrypt_portal_password,
    get_user_id_from_token,
)
from playwright_service import fetch_grid_data, make_reservation, close_browser

# ── Logging ────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "frontend"

# ── Lifespan ───────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield
    await close_browser()
    logger.info("Browser closed")


app = FastAPI(title="FitAcademie Squash Webapp", lifespan=lifespan)

# Mount static frontend
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Security helper ────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    session_token: Optional[str] = Cookie(None),
) -> User:
    """Extract user from JWT in Authorization header or session cookie."""
    token = None
    if credentials:
        token = credentials.credentials
    elif session_token:
        token = session_token

    if not token:
        raise HTTPException(status_code=401, detail="Niet ingelogd")

    user_id = get_user_id_from_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Ongeldige sessie")

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=401, detail="Gebruiker niet gevonden")
        return user


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
    weekday: int  # 0=Mon..6=Sun
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
        if v not in ("weekly", "biweekly", "cron"):
            raise ValueError("frequency must be weekly, biweekly, or cron")
        return v


class ReservationRequest(BaseModel):
    court: int
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    partner_id: int  # References partner in DB


# ── Auth Routes ────────────────────────────────────────────────────


@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response):
    """Authenticate user. Stores local password, FitAcademie credentials encrypted."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == req.email))
        user = result.scalar_one_or_none()

        if user:
            if not verify_password(req.password, user.password_hash):
                raise HTTPException(401, "Onjuist wachtwoord")
        else:
            # Create new user
            user = User(
                email=req.email,
                password_hash=hash_password(req.password),
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

        # Update FitAcademie credentials
        if req.fitacademie_email:
            user.fitacademie_email = req.fitacademie_email
        if req.fitacademie_password:
            user.fitacademie_password_enc = encrypt_portal_password(req.fitacademie_password)
        await db.commit()

        token = create_access_token(
            user.id, user.email,
            timedelta(days=30 if req.remember else 1)
        )

        response.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            secure=False,  # True in production with HTTPS
            samesite="lax",
            max_age=30 * 24 * 3600 if req.remember else 24 * 3600,
        )

        return {
            "success": True,
            "user": {"id": user.id, "email": user.email},
            "token": token,
        }


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("session_token")
    return {"success": True}


@app.get("/api/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "fitacademie_email": user.fitacademie_email,
        "has_fitacademie_creds": bool(user.fitacademie_password_enc),
        "cookies_valid": bool(user.session_cookies),
    }


# ── Grid route ─────────────────────────────────────────────────────


@app.get("/api/grid")
async def grid(
    start: str = Query(default=None, description="YYYY-MM-DD"),
    days: int = Query(default=7, ge=1, le=14),
    user: User = Depends(get_current_user),
):
    """Fetch squash availability grid."""
    if not user.fitacademie_password_enc:
        raise HTTPException(400, "FitAcademie credentials not set — login first with portal credentials")

    if not start:
        start = date.today().isoformat()

    data = await fetch_grid_data(user, start, days)
    if "error" in data:
        raise HTTPException(502, data["error"])
    return data


# ── Reservation route ──────────────────────────────────────────────


@app.post("/api/reserve")
async def reserve(
    req: ReservationRequest,
    user: User = Depends(get_current_user),
):
    """Make a squash reservation using a saved partner."""
    async with AsyncSessionLocal() as db:
        partner = await db.get(Partner, req.partner_id)
        if not partner or partner.user_id != user.id:
            raise HTTPException(404, "Partner niet gevonden")

    result = await make_reservation(
        user,
        court=req.court,
        date_str=req.date,
        time_str=req.time,
        partner_email=partner.email,
        partner_is_bepalend=partner.is_bepalend_lid,
    )

    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)

    # Save to reservation history
    async with AsyncSessionLocal() as db:
        res = Reservation(
            user_id=user.id,
            partner_id=req.partner_id,
            court=req.court,
            date=datetime.strptime(req.date, "%Y-%m-%d").date(),
            time=req.time,
            status="booked",
            amount_paid=0.0,
        )
        db.add(res)
        await db.commit()

    return result


# ── Partner CRUD ───────────────────────────────────────────────────


@app.get("/api/partners")
async def list_partners(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Partner).where(Partner.user_id == user.id).order_by(Partner.name)
        )
        partners = result.scalars().all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "email": p.email,
                "is_bepalend_lid": p.is_bepalend_lid,
                "notes": p.notes,
            }
            for p in partners
        ]


@app.post("/api/partners")
async def create_partner(req: PartnerCreate, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        partner = Partner(
            user_id=user.id,
            name=req.name,
            email=req.email,
            is_bepalend_lid=req.is_bepalend_lid,
            notes=req.notes,
        )
        db.add(partner)
        await db.commit()
        await db.refresh(partner)
        return {"id": partner.id, **req.model_dump()}


@app.patch("/api/partners/{partner_id}")
async def update_partner(partner_id: int, req: PartnerUpdate, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        partner = await db.get(Partner, partner_id)
        if not partner or partner.user_id != user.id:
            raise HTTPException(404, "Partner niet gevonden")

        if req.name is not None:
            partner.name = req.name
        if req.email is not None:
            partner.email = req.email
        if req.is_bepalend_lid is not None:
            partner.is_bepalend_lid = req.is_bepalend_lid
        if req.notes is not None:
            partner.notes = req.notes

        await db.commit()
        return {"success": True}


@app.delete("/api/partners/{partner_id}")
async def delete_partner(partner_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        partner = await db.get(Partner, partner_id)
        if not partner or partner.user_id != user.id:
            raise HTTPException(404, "Partner niet gevonden")
        await db.delete(partner)
        await db.commit()
        return {"success": True}


# ── Recurring bookings ─────────────────────────────────────────────


@app.get("/api/recurring")
async def list_recurring(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(RecurringBooking)
            .where(RecurringBooking.user_id == user.id)
            .order_by(RecurringBooking.weekday, RecurringBooking.time)
        )
        bookings = result.scalars().all()
        return [
            {
                "id": b.id,
                "partner_id": b.partner_id,
                "court": b.court,
                "weekday": b.weekday,
                "time": b.time,
                "frequency": b.frequency,
                "active": b.active,
                "next_run": b.next_run.isoformat() if b.next_run else None,
            }
            for b in bookings
        ]


@app.post("/api/recurring")
async def create_recurring(req: RecurringCreate, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        partner = await db.get(Partner, req.partner_id)
        if not partner or partner.user_id != user.id:
            raise HTTPException(404, "Partner niet gevonden")

        booking = RecurringBooking(
            user_id=user.id,
            partner_id=req.partner_id,
            court=req.court,
            weekday=req.weekday,
            time=req.time,
            frequency=req.frequency,
            active=True,
        )

        # Calculate next_run
        today = date.today()
        days_ahead = (req.weekday - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # Next week
        next_date = today + timedelta(days=days_ahead)
        booking.next_run = datetime.combine(
            next_date,
            datetime.strptime(req.time, "%H:%M").time(),
        )

        db.add(booking)
        await db.commit()
        return {"success": True, "id": booking.id}


@app.patch("/api/recurring/{booking_id}")
async def toggle_recurring(booking_id: int, active: bool = Query(...), user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        booking = await db.get(RecurringBooking, booking_id)
        if not booking or booking.user_id != user.id:
            raise HTTPException(404, "Boeking niet gevonden")
        booking.active = active
        await db.commit()
        return {"success": True}


@app.delete("/api/recurring/{booking_id}")
async def delete_recurring(booking_id: int, user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        booking = await db.get(RecurringBooking, booking_id)
        if not booking or booking.user_id != user.id:
            raise HTTPException(404, "Boeking niet gevonden")
        await db.delete(booking)
        await db.commit()
        return {"success": True}


@app.get("/health")
async def health():
    return {"status": "ok", "db": "sqlite"}


# ── SPA fallback ───────────────────────────────────────────────────


@app.get("/")
@app.get("/login")
async def serve_spa():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text())
    return HTMLResponse("<h1>Frontend not built — run the webapp setup</h1>")


# ── Entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload_enabled = os.getenv("RELOAD", "false").lower() == "true"
    uvicorn.run("main:app", host=host, port=port, reload=reload_enabled)
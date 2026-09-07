"""FastAPI backend for FitAcademie Squash Webapp."""
import json
import logging
import os
import sys
import asyncio
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
from fitacademie_api import FitAcademieClient

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
    logger.info("App shutting down")


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


# ── FA client cache ─────────────────────────────────────────────────

_client_cache: dict[int, FitAcademieClient] = {}


def get_fa_client(user: User) -> FitAcademieClient:
    """Get or create a FitAcademieClient. Uses cookies if available."""
    uid = user.id
    if uid in _client_cache:
        client = _client_cache[uid]
        if client.logged_in:
            return client

    if not user.fitacademie_email:
        raise HTTPException(400, "FitAcademie email not set")

    fa_pass = user.fitacademie_password_enc
    password = decrypt_portal_password(fa_pass) if fa_pass else None

    if not password and not user.session_cookies:
        raise HTTPException(400, "FitAcademie credentials not set — login via app met portal credentials")

    client = FitAcademieClient(user.fitacademie_email, password or "")

    if user.session_cookies:
        try:
            client.set_cookies(json.loads(user.session_cookies))
        except Exception:
            pass

    if not client.login():  # cookies failed or expired -> full login
        if not password:
            raise HTTPException(502, "FitAcademie login mislukt — cookies expired en geen wachtwoord")
        client = FitAcademieClient(user.fitacademie_email, password)
        if not client.login():
            raise HTTPException(502, "FitAcademie login mislukt — check portal credentials")

    # Save session cookies for next time
    user.session_cookies = json.dumps(client.get_cookies())

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

    @field_validator("email")
    @classmethod
    def no_test_domains(cls, v: str) -> str:
        test_domains = {"test.com", "example.com", "dummy.com", "localhost", "test.nl", "example.nl"}
        domain = v.split("@")[-1].lower()
        if domain in test_domains:
            raise ValueError("Geen test-domeinen toegestaan voor partners")
        return v


class PartnerUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    is_bepalend_lid: Optional[bool] = None
    notes: Optional[str] = None

    @field_validator("email")
    @classmethod
    def no_test_domains(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        test_domains = {"test.com", "example.com", "dummy.com", "localhost", "test.nl", "example.nl"}
        domain = v.split("@")[-1].lower()
        if domain in test_domains:
            raise ValueError("Geen test-domeinen toegestaan voor partners")
        return v


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
    """Fetch squash availability grid using pure-Python client."""
    if not user.fitacademie_password_enc:
        raise HTTPException(400, "FitAcademie credentials not set — login first with portal credentials")

    if not start:
        start = date.today().isoformat()

    loop = asyncio.get_event_loop()
    try:
        client = get_fa_client(user)
        slots = await loop.run_in_executor(None, client.get_grid, start, days)
        return {"slots": slots, "start_date": start, "days": days}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Grid fetch failed")
        raise HTTPException(502, str(e))


# ── Reservation route ──────────────────────────────────────────────


@app.post("/api/reserve")
async def reserve(
    req: ReservationRequest,
    user: User = Depends(get_current_user),
):
    """Make a squash reservation using the pure-Python client."""
    async with AsyncSessionLocal() as db:
        partner = await db.get(Partner, req.partner_id)
        if not partner or partner.user_id != user.id:
            raise HTTPException(404, "Partner niet gevonden")

    loop = asyncio.get_event_loop()

    def _do_reserve():
        client = get_fa_client(user)
        # First find the slot_id for this court/date/time
        slots = client.get_grid(req.date, 1)
        target = None
        for s in slots:
            if s["court"] == req.court and s["start"] == req.time:
                target = s
                break
        if not target:
            raise ValueError(f"Slot niet gevonden: baan {req.court}, {req.date} {req.time}")
        if not target.get("available", True):
            raise ValueError(f"Slot is niet beschikbaar ({target.get('booked', 0)}/{target.get('capacity', 2)})")

        # Check partner price
        check = client.check_partner(target["slot_id"], partner.email)

        # AUDIT LOG
        logger.info(
            "RESERVE_ATTEMPT",
            extra={
                "user_id": user.id,
                "slot_id": target["slot_id"],
                "partner_email": partner.email,
                "partner_is_bepalend": partner.is_bepalend_lid,
                "endpoint": "add_zero_price" if check["price"] == 0 else "add",
                "court": req.court,
                "date": req.date,
                "time": req.time,
            }
        )

        # Reserve (will raise ValueError if partner invalid)
        result = client.reserve(target["slot_id"], partner.email)
        if not result.get("success"):
            raise ValueError(result.get("error", "Reserveren mislukt"))

        return {
            "success": True,
            "court": req.court,
            "date": req.date,
            "time": req.time,
            "partner": partner.email,
            "partner_price": check["price"],
            "partner_valid": check["valid"],
            "cart_items": result.get("cart_items"),
            "cart_total": result.get("cart_total"),
        }

    try:
        result = await loop.run_in_executor(None, _do_reserve)
    except ValueError as e:
        logger.warning(f"RESERVE_FAILED: user={user.id} error={e}")
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Reservation failed")
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

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
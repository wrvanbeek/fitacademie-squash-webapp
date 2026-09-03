"""
Pure-Python FitAcademie API client — no Playwright, no browser.

Uses requests + BeautifulSoup to interact with the FitAcademie portal.
Can be used on any Python platform (Vercel, Railway, Cloudflare Workers via Python).
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, date
from typing import Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://portaal.fitacademie.nl"

# Realistic browser headers to avoid IP-based blocking
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Dnt": "1",
    "Connection": "keep-alive",
}


class FitAcademieClient:
    """Lightweight client for FitAcademie portal."""

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.session.cookies.update({
            "cookieconsent_status": "dismiss",
            "cookiesession": "1",
        })
        self.logged_in = False
        self._csrf_token = None

    # ── Login ────────────────────────────────────────────────────────

    def login(self) -> bool:
        """Log in to the FitAcademie portal. Returns True on success."""
        # First visit to get cookies and check if logged in
        r = self.session.get(f"{BASE_URL}/club_portal/lessons", timeout=30)
        
        # Check if portal responded
        if r.status_code == 403:
            logger.error("Portal returned 403 Forbidden — server IP blocked")
            return False
            
        r.raise_for_status()

        # Check if already logged in
        if "Uitloggen" in r.text or "Welkom" in r.text:
            self.logged_in = True
            logger.info("Already logged in")
            return True

        # Submit login form
        login_data = {
            "email": self.email,
            "password": self.password,
            "commit": "INLOGGEN",
        }
        r = self.session.post(
            f"{BASE_URL}/club_portal/",
            data=login_data,
            headers={"Referer": f"{BASE_URL}/club_portal/lessons"},
            timeout=30,
        )
        r.raise_for_status()

        # Check login success
        if "Uitloggen" in r.text or "Welkom" in r.text:
            self.logged_in = True
            logger.info(f"Login successful for {self.email}")
            return True

        logger.error("Login failed — check credentials")
        return False

    def ensure_login(self):
        """Ensure we're logged in, login if needed."""
        if not self.logged_in:
            if not self.login():
                raise RuntimeError("Login failed")

    # ── Grid data ────────────────────────────────────────────────────

    def get_slots_for_day(self, day_offset: int = 0) -> list[dict]:
        """Fetch squash slots for a specific day offset (0=today, 1=tomorrow, etc.).

        Returns list of {court, start, end, booked, capacity, available, slot_id, date}
        """
        self.ensure_login()

        # The day links are /club_portal/lessons/{offset}?
        day_url = f"{BASE_URL}/club_portal/lessons/{day_offset}?"
        r = self.session.get(day_url, timeout=30)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        slots = []

        # Find all <li> elements that contain squash slots
        for li in soup.select("li"):
            h3 = li.find("h3")
            if not h3 or "SQUASHBAAN" not in h3.get_text(strip=True).upper():
                continue

            # Extract court number
            court_match = re.search(r'Squashbaan\s*(\d)', h3.get_text(strip=True), re.IGNORECASE)
            if not court_match:
                continue
            court = int(court_match.group(1))

            # Extract time
            dtime = li.select_one(".d_time")
            if not dtime:
                continue
            time_match = re.search(r'(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})', dtime.get_text(strip=True))
            if not time_match:
                continue

            # Extract capacity
            text = li.get_text()
            cap_match = re.search(r'(\d+)\s*/\s*(\d+)', text)
            price_match = re.search(r'€\s*([\d,.]+)', text)

            # Extract slot ID from reserve button or "Meer info" link
            slot_id = None
            reserve_btn = li.find("a", id=lambda x: x and "reserve_" in x)
            if reserve_btn and reserve_btn.get("id"):
                id_match = re.search(r"reserve_(\d+)", str(reserve_btn.get("id")))
                if id_match:
                    slot_id = int(id_match.group(1))
            if not slot_id:
                # Fallback: extract from "Meer info" link's res_table_id
                meer_info = li.find("a", href=re.compile(r"res_table_id=(\d+)"))
                if meer_info and meer_info.get("href"):
                    id_match = re.search(r"res_table_id=(\d+)", meer_info["href"])
                    if id_match:
                        slot_id = int(id_match.group(1))

            booked = int(cap_match.group(1)) if cap_match else 0
            capacity = int(cap_match.group(2)) if cap_match else 2

            slots.append({
                "court": court,
                "start": time_match.group(1),
                "end": time_match.group(2),
                "booked": booked,
                "capacity": capacity,
                "available": booked < capacity,
                "price": price_match.group(1) if price_match else "0.00",
                "slot_id": slot_id,
            })

        return slots

    def get_grid(self, start_date: Optional[str] = None, days: int = 7) -> list[dict]:
        """Fetch slots for multiple days.

        Args:
            start_date: YYYY-MM-DD or None for today
            days: Number of days to fetch

        Returns flat list of slot dicts with 'date' field added.
        """
        if start_date:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
        else:
            start = datetime.now().date()

        # Calculate base day offset from today
        today = datetime.now().date()
        base_offset = (start - today).days

        all_slots = []
        for offset in range(days):
            day_offset = base_offset + offset
            date = start + timedelta(days=offset)
            slots = self.get_slots_for_day(day_offset)
            for s in slots:
                s["date"] = date.isoformat()
                s["day_label"] = date.strftime("%a %d %b")
                s["weekday"] = date.weekday()
            all_slots.extend(slots)
            time.sleep(0.5)  # Be nice

        return all_slots

    # ── Partner validation ──────────────────────────────────────────

    def check_partner(self, slot_id: int, partner_email: str) -> dict:
        """Validate a partner email for a specific slot.

        Returns dict with {
            "valid": bool,        # True if known member
            "price": float,       # 0.0 for known member, 12.5 for guest
            "message": str,       # Human-readable status
        }
        """
        self.ensure_login()

        r = self.session.post(
            f"{BASE_URL}/reservation_activities/check_aditional_player/{slot_id}",
            data={
                "player_email": partner_email,
                "idx": "0",
                "birthday": "",
                "team_size": "1",
            },
            headers={
                "Referer": f"{BASE_URL}/club_portal/lessons",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )
        r.raise_for_status()

        # Response is Prototype.js style JavaScript
        body = r.text

        # Check if valid
        is_valid = "Geldige speler" in body
        is_unknown = "Onbekende" in body or "niet geldig" in body

        # Extract price
        price_match = re.search(r"value='([\d.]+)'", body)
        price = float(price_match.group(1)) if price_match else 0.0

        if is_valid:
            return {"valid": True, "price": price, "message": "Geldige speler, geen extra kosten"}
        elif is_unknown:
            return {"valid": False, "price": price, "message": f"Onbekende speler, €{price:.2f} extra"}
        else:
            # Try to determine from price
            if price > 0:
                return {"valid": False, "price": price, "message": f"Onbekende speler, €{price:.2f} extra"}
            return {"valid": True, "price": 0, "message": "Geldige speler (price-based)"}

    # ── Reservation ─────────────────────────────────────────────────

    def reserve(self, slot_id: int, partner_email: str,
                team_size: int = 2, with_page_reload: bool = True) -> dict:
        """Make a reservation for a slot.

        Args:
            slot_id: The slot ID (from get_slots_for_day)
            partner_email: Partner's email
            team_size: 2 (default) or 4
            with_page_reload: True to reload page after (default)

        Returns dict with {"success": bool, "cart_url": str, ...}
        """
        self.ensure_login()

        # First check the partner price
        check = self.check_partner(slot_id, partner_email)
        full_price = check["price"]

        # Choose endpoint based on price
        if full_price > 0.0:
            endpoint = f"/cart/add/{slot_id}"
        else:
            endpoint = f"/cart/add_zero_price/{slot_id}"

        form_params = {
            "with_page_reload": 1 if with_page_reload else 0,
            "type": "a_reservation_tables",
            "team_size": str(team_size),
            "players": partner_email,
        }

        # Also include the authorized user (our email)
        # The JS does this automatically via the form, we need to include it
        r = self.session.post(
            f"{BASE_URL}{endpoint}",
            data=form_params,
            headers={
                "Referer": f"{BASE_URL}/club_portal/lessons",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )

        if r.status_code == 200:
            logger.info(f"Reservation added to cart: slot {slot_id}, partner {partner_email}")

            # Get cart info from the response (if HTML with_page_reload)
            cart_items = None
            cart_total = None
            if with_page_reload:
                soup = BeautifulSoup(r.text, "html.parser")
                cart_count = soup.select_one(".cartbox .digits")
                if cart_count:
                    cart_items = cart_count.get_text(strip=True)
                total_price = soup.select_one("#cart_total_price")
                if total_price:
                    cart_total = total_price.get_text(strip=True)

            return {
                "success": True,
                "slot_id": slot_id,
                "partner": partner_email,
                "partner_price": full_price,
                "cart_items": cart_items,
                "cart_total": cart_total,
                "endpoint": endpoint,
            }
        else:
            logger.error(f"Reservation failed: HTTP {r.status_code}")
            return {"success": False, "error": f"HTTP {r.status_code}", "body": r.text[:500]}

    # ── Session state ───────────────────────────────────────────────

    def get_cookies(self) -> list[dict]:
        """Get current session cookies for serialization."""
        return [
            {"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path, "secure": c.secure}
            for c in self.session.cookies
        ]

    def set_cookies(self, cookies: list[dict]):
        """Restore cookies from a previously saved state."""
        for c in cookies:
            self.session.cookies.set(c["name"], c["value"],
                                      domain=c.get("domain", "portaal.fitacademie.nl"),
                                      path=c.get("path", "/"))

    def close(self):
        """Close the session."""
        self.session.close()
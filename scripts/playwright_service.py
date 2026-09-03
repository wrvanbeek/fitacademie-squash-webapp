"""Playwright automation service for FitAcademie portal."""
import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("FITACADEMIE_BASE_URL", "https://portaal.fitacademie.nl/club_portal/lessons")

_playwright = None
_browser = None


async def _get_browser():
    global _playwright, _browser
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
    return _browser


async def _random_delay(min_s=0.5, max_s=2.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


# ── Cookie management ──────────────────────────────────────────────


async def restore_or_login(page, user):
    """Restore Playwright cookies or login fresh."""
    from auth import decrypt_portal_password

    if user.session_cookies:
        try:
            await page.context.clear_cookies()
            cookies = json.loads(user.session_cookies)
            await page.context.add_cookies(cookies)
        except Exception as e:
            logger.warning(f"Cookie restore failed: {e}")

    await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
    await _random_delay()

    if await page.locator(".current_user_info").count() > 0:
        return True

    portal_pass = decrypt_portal_password(user.fitacademie_password_enc)
    await page.locator('#email').fill(user.fitacademie_email)
    await page.locator('#password').fill(portal_pass)
    await _random_delay()
    await page.locator('input[value="INLOGGEN"]').click()
    await _random_delay()
    await page.wait_for_load_state("networkidle", timeout=15000)

    if await page.locator(".current_user_info").count() > 0:
        await _save_cookies(page, user.id)
        return True
    return False


async def _save_cookies(page, user_id):
    from models import User
    from database import AsyncSessionLocal
    try:
        state = await page.context.storage_state()
        async with AsyncSessionLocal() as session:
            u = await session.get(User, int(user_id))
            if u:
                u.session_cookies = json.dumps(state.get("cookies", []))
                u.cookies_updated = datetime.now(timezone.utc)
                await session.commit()
    except Exception as e:
        logger.warning(f"Save cookies failed: {e}")


# ── Grid data ──────────────────────────────────────────────────────


async def fetch_grid_data(user, start_date: str, days: int = 7) -> dict:
    """Fetch squash availability grid for a date range."""
    browser = await _get_browser()
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, locale="nl-NL"
    )
    page = await context.new_page()
    try:
        if not await restore_or_login(page, user):
            return {"error": "Login mislukt"}

        start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else datetime.now().date()
        all_slots = []

        for day_offset in range(days):
            date = start + timedelta(days=day_offset)
            await _go_to_day(page, day_offset)
            slots = await _parse_slots(page)
            for s in slots:
                s["date"] = date.isoformat()
                s["day_label"] = date.strftime("%a %d %b")
                s["weekday"] = date.weekday()
            all_slots.extend(slots)
            await _random_delay()

        return {"slots": all_slots, "start_date": start_date, "days": days}
    except Exception as e:
        logger.exception("Grid fetch failed")
        return {"error": str(e)}
    finally:
        await context.close()
        await _save_cookies(page, user.id)


async def _go_to_day(page, day_offset: int):
    links = page.locator("a.allweekb, a[href*='/lessons/']")
    for i in range(await links.count()):
        href = await links.nth(i).get_attribute("href") or ""
        m = re.search(r'/lessons/(\d+)', href)
        if m and int(m.group(1)) == day_offset:
            await links.nth(i).click()
            await asyncio.sleep(1)
            await page.wait_for_load_state("networkidle", timeout=10000)
            return True
    return False


async def _parse_slots(page) -> list:
    """Parse squash slots from the page <li> elements."""
    slots = []
    for i in range(await page.locator("li").count()):
        li = page.locator("li").nth(i)
        h3 = li.locator("h3")
        if await h3.count() == 0:
            continue
        title = (await h3.inner_text()).strip()
        if "SQUASHBAAN" not in title.upper():
            continue
        cm = re.search(r'Squashbaan\s*(\d)', title, re.IGNORECASE)
        if not cm:
            continue
        court = int(cm.group(1))

        dtime = li.locator(".d_time")
        if await dtime.count() == 0:
            continue
        tm = re.search(r'(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})', await dtime.inner_text())
        if not tm:
            continue

        text = await li.inner_text()
        cap = re.search(r'(\d+)\s*/\s*(\d+)', text)
        price = re.search(r'€\s*([\d,.]+)', text)

        booked = int(cap.group(1)) if cap else 0
        capacity = int(cap.group(2)) if cap else 2

        slots.append({
            "court": court,
            "start": tm.group(1),
            "end": tm.group(2),
            "booked": booked,
            "capacity": capacity,
            "available": booked < capacity,
            "price": price.group(1) if price else "0.00",
        })
    return slots


# ── Reservation ────────────────────────────────────────────────────


async def make_reservation(user, court: int, date_str: str, time_str: str,
                           partner_email: str, partner_is_bepalend: bool = False) -> dict:
    """Make a squash reservation on the portal."""
    browser = await _get_browser()
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, locale="nl-NL"
    )
    page = await context.new_page()
    try:
        if not await restore_or_login(page, user):
            return {"success": False, "error": "Login mislukt"}

        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        day_offset = (target_date - datetime.now().date()).days
        if not await _go_to_day(page, day_offset):
            return {"success": False, "error": f"Kan dag {date_str} niet vinden"}

        await _random_delay()

        # Find the right court slot
        target_li = None
        for i in range(await page.locator("li").count()):
            li = page.locator("li").nth(i)
            h3 = li.locator("h3")
            if await h3.count() == 0:
                continue
            title = (await h3.inner_text()).strip()
            if "SQUASHBAAN" not in title.upper():
                continue
            cm = re.search(r'Squashbaan\s*(\d)', title, re.IGNORECASE)
            if not cm or int(cm.group(1)) != court:
                continue
            if time_str in await li.inner_text():
                target_li = li
                break

        if target_li is None:
            return {"success": False, "error": f"Slot niet gevonden: baan {court}, {date_str} {time_str}"}

        # Check capacity
        text = await target_li.inner_text()
        cap = re.search(r'(\d+)\s*/\s*(\d+)', text)
        if cap and int(cap.group(1)) >= int(cap.group(2)):
            return {"success": False, "error": f"Slot vol ({cap.group(1)}/{cap.group(2)})"}

        # Fill partner
        email_input = target_li.locator('input.player_of_team, input[name="partners[]"]')
        if await email_input.count() == 0:
            return {"success": False, "error": "Geen partner-emailveld gevonden"}

        await email_input.fill(partner_email)
        await asyncio.sleep(2)

        # Check partner validation
        info_span = target_li.locator("span[id*='player_info']:not([id$='_10'])")
        partner_valid = True
        if await info_span.count() > 0:
            info_text = await info_span.inner_text()
            if "Onbekende" in info_text or "niet geldig" in info_text:
                partner_valid = False

        # Click Inschrijven
        reserve_btn = target_li.locator('a:has-text("Inschrijven"), a[id*="reserve_"]')
        cls = await reserve_btn.get_attribute("class") or ""
        if "invisible" in cls:
            # Try to make it visible via JS
            await page.evaluate("""() => {
                const btn = document.querySelector('a[id*="reserve_"]');
                if (btn) { btn.classList.remove('invisible', 'r_inactive'); }
            }""")
            await asyncio.sleep(1)

        if await reserve_btn.count() == 0:
            return {"success": False, "error": "Geen 'Inschrijven' knop gevonden"}

        await reserve_btn.click()
        await _random_delay()
        await page.wait_for_load_state("networkidle", timeout=10000)

        # Verify
        cart_count = ""
        if await page.locator(".cartbox .digits").count() > 0:
            cart_count = await page.locator(".cartbox .digits").inner_text()
        cart_total = ""
        if await page.locator("#cart_total_price").count() > 0:
            cart_total = await page.locator("#cart_total_price").inner_text()

        return {
            "success": True,
            "court": court,
            "date": date_str,
            "time": time_str,
            "partner": partner_email,
            "partner_validated": partner_valid,
            "cart_items": cart_count,
            "cart_total": cart_total,
        }
    except Exception as e:
        logger.exception("Reservation failed")
        return {"success": False, "error": str(e)}
    finally:
        await context.close()
        await _save_cookies(page, user.id)


async def close_browser():
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib import request

import yaml
import jpholiday
from playwright.async_api import async_playwright, Page

BASE_URL = "https://www.pf489.com/machida/web/?DisableSideMenu=true&PurposeCode=41&SSCategory=01&StartPage=ShisetsuSentaku"
STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")


def load_config() -> dict:
    with open("config.yml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def discord_notify(webhook: str, message: str) -> None:
    if not webhook:
        print("[notify skipped] DISCORD_WEBHOOK_URL is not set")
        print(message)
        return
    payload = json.dumps({"content": message}, ensure_ascii=False).encode("utf-8")
    req = request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Discord webhook failed: HTTP {resp.status}")


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except Exception:
        return set()


def save_state(keys: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"seen": sorted(keys), "updated_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def click_next(page: Page) -> None:
    candidates = ["次へ", "検索", "表示", "決定", "選択"]
    for name in candidates:
        loc = page.get_by_role("button", name=re.compile(name)).last
        if await loc.count():
            await loc.click()
            await page.wait_for_load_state("networkidle")
            return
        loc2 = page.get_by_role("link", name=re.compile(name)).last
        if await loc2.count():
            await loc2.click()
            await page.wait_for_load_state("networkidle")
            return
    # ASP.NET pages often use submit/image inputs with Japanese alt/value text.
    for sel in ["input[type=submit]", "input[type=image]", "button"]:
        elems = page.locator(sel)
        for i in range(await elems.count()):
            el = elems.nth(i)
            txt = " ".join(filter(None, [await el.get_attribute("value"), await el.get_attribute("alt"), await el.inner_text() if await el.is_visible() else ""]))
            if any(x in txt for x in candidates):
                await el.click()
                await page.wait_for_load_state("networkidle")
                return
    raise RuntimeError("次へ進むボタンを見つけられませんでした。debug/ の画面を確認してください。")


async def select_facilities(page: Page, facility_keywords: list[str]) -> None:
    """Select requested facilities, following the site's facility-list pagination.

    Machida's legacy ASP.NET screen renders each facility as an
    ``input[type=submit]`` whose *value* is the facility name.  It is not a
    checkbox in the DOM, so body text / checkbox selectors do not see it.
    The list is also split across pages (e.g. 野津田公園北 is on page 2).
    """
    remaining = list(dict.fromkeys(facility_keywords))
    selected: list[str] = []

    for _ in range(10):  # safety cap for facility-list pagination
        # The facility controls expose their value as the accessible button name.
        for keyword in remaining[:]:
            loc = page.get_by_role("button", name=keyword, exact=True)
            if await loc.count():
                await loc.first.click()
                await page.wait_for_timeout(150)
                selected.append(keyword)
                remaining.remove(keyword)
                print(f"[selected] {keyword}")

        if not remaining:
            break

        # Some target facilities are on the next facility-list page.
        next_page = page.locator("#btnNextPage:not([disabled])")
        if not await next_page.count():
            break

        before = await page.locator("#lblPage").inner_text() if await page.locator("#lblPage").count() else ""
        await next_page.click()
        await page.wait_for_load_state("networkidle")
        after = await page.locator("#lblPage").inner_text() if await page.locator("#lblPage").count() else ""
        print(f"[facility page] {before} -> {after}")

    if remaining:
        raise RuntimeError(
            "指定施設を選択できませんでした: " + ", ".join(remaining) +
            "。debug/01_facility.* を確認してください。"
        )

    if not selected and facility_keywords:
        raise RuntimeError("指定施設を1件も選択できませんでした。")


async def set_date_if_possible(page: Page, target: datetime) -> None:
    # Best-effort support for common date selects on legacy ASP.NET screens.
    selects = page.locator("select")
    values = {
        "year": [str(target.year), f"{target.year}年"],
        "month": [str(target.month), f"{target.month:02d}", f"{target.month}月"],
        "day": [str(target.day), f"{target.day:02d}", f"{target.day}日"],
    }
    for i in range(await selects.count()):
        sel = selects.nth(i)
        name = ((await sel.get_attribute("name")) or "").lower()
        sid = ((await sel.get_attribute("id")) or "").lower()
        hint = name + " " + sid
        kind = None
        if any(x in hint for x in ["year", "yyyy", "nen"]): kind = "year"
        elif any(x in hint for x in ["month", "mm", "gatsu"]): kind = "month"
        elif any(x in hint for x in ["day", "dd", "nichi"]): kind = "day"
        if not kind:
            continue
        opts = [normalize(x) for x in await sel.locator("option").all_text_contents()]
        for candidate in values[kind]:
            match = next((o for o in opts if o == candidate or candidate in o), None)
            if match:
                try:
                    await sel.select_option(label=match)
                    break
                except Exception:
                    pass


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":", 1)
    return time(int(h), int(m))


def _extract_date(text: str, today: date) -> date | None:
    """Best-effort extraction of a date shown in a result row."""
    patterns = [
        r"(?P<y>20\d{2})[年/\-.](?P<m>\d{1,2})[月/\-.](?P<d>\d{1,2})日?",
        r"(?<!\d)(?P<m>\d{1,2})[月/\-.](?P<d>\d{1,2})日?(?!\d)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        year = int(m.groupdict().get("y") or today.year)
        month = int(m.group("m"))
        day = int(m.group("d"))
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        # If a year-less date looks far in the past, it likely belongs to next year.
        if "y" not in m.groupdict() or not m.groupdict().get("y"):
            if candidate < today - timedelta(days=60):
                try:
                    candidate = date(year + 1, month, day)
                except ValueError:
                    pass
        return candidate
    return None


def _extract_time_ranges(text: str) -> list[tuple[time, time]]:
    """Extract common forms such as 19:00-21:00 or 19時～21時."""
    pat = re.compile(
        r"(?P<h1>[0-2]?\d)(?:[:時](?P<m1>[0-5]\d)?)?\s*"
        r"(?:-|－|−|~|〜|～|から)\s*"
        r"(?P<h2>[0-2]?\d)(?:[:時](?P<m2>[0-5]\d)?)?"
    )
    out = []
    for m in pat.finditer(text):
        h1, h2 = int(m.group("h1")), int(m.group("h2"))
        m1, m2 = int(m.group("m1") or 0), int(m.group("m2") or 0)
        if h1 <= 23 and h2 <= 23:
            out.append((time(h1, m1), time(h2, m2)))
    return out


def matches_requested_schedule(row_text: str, cfg: dict, today: date | None = None) -> bool:
    """Apply: weekends/Japanese holidays=all day; weekdays=configured time ranges."""
    today = today or datetime.now().date()
    schedule = cfg.get("schedule", {})
    row_date = _extract_date(row_text, today)
    ranges = _extract_time_ranges(row_text)

    # If the row date is available, decide weekend/holiday vs weekday precisely.
    if row_date is not None:
        is_dayoff = row_date.weekday() >= 5 or jpholiday.is_holiday(row_date)
        if is_dayoff and schedule.get("weekends_and_holidays", {}).get("all_day", True):
            return True

        wanted = schedule.get("weekdays", {}).get("time_ranges", [])
        if not wanted:
            return False
        if not ranges:
            # Do not treat an unknown-time weekday row as a match.
            return False
        for actual_start, actual_end in ranges:
            for item in wanted:
                wanted_start = _parse_hhmm(item["start"])
                wanted_end = _parse_hhmm(item["end"])
                if actual_start == wanted_start and actual_end == wanted_end:
                    return True
        return False

    # Date is not printed in the row. We can still safely identify the requested
    # weekday evening slot by its time; otherwise leave the row in place so a
    # weekend/holiday all-day opening is not accidentally missed.
    wanted = schedule.get("weekdays", {}).get("time_ranges", [])
    for actual_start, actual_end in ranges:
        for item in wanted:
            if actual_start == _parse_hhmm(item["start"]) and actual_end == _parse_hhmm(item["end"]):
                return True
    return not ranges


async def scrape_available_rows(page: Page, cfg: dict) -> list[str]:
    text = normalize(await page.locator("body").inner_text())
    available_tokens = cfg.get("available_tokens", ["○", "空き", "予約可"])
    facility_keywords = cfg.get("facility_keywords", [])

    results = []
    rows = page.locator("tr")
    for i in range(await rows.count()):
        rowtxt = normalize(await rows.nth(i).inner_text())
        if not rowtxt:
            continue
        if not any(tok in rowtxt for tok in available_tokens):
            continue
        if facility_keywords and not any(k in rowtxt for k in facility_keywords):
            # Many result screens place facility in a header rather than every row; allow if whole page contains it.
            if not any(k in text for k in facility_keywords):
                continue
        if not matches_requested_schedule(rowtxt, cfg):
            continue
        # Ignore legend/explanation rows.
        if len(rowtxt) < 3 or len(rowtxt) > 500:
            continue
        results.append(rowtxt)

    # Fallback: useful for non-table mobile-ish result layouts.
    if not results:
        lines = [normalize(x) for x in (await page.locator("body").inner_text()).splitlines()]
        for idx, line in enumerate(lines):
            if any(tok in line for tok in available_tokens):
                context = " | ".join(lines[max(0, idx-2): idx+3])
                if matches_requested_schedule(context, cfg):
                    results.append(context)

    return sorted(set(results))


async def inspect_page(page: Page, label: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    await page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
    (DEBUG_DIR / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    (DEBUG_DIR / f"{label}.txt").write_text(await page.locator("body").inner_text(), encoding="utf-8")



async def choose_two_week_view(page: Page) -> None:
    """Use a 2-week result grid so the configured 14-day horizon fits on one screen."""
    btn = page.locator("#rbtnTwoWeek")
    if await btn.count():
        cls = (await btn.get_attribute("class")) or ""
        if "Orange" not in cls:
            await btn.click()
            await page.wait_for_load_state("networkidle")
            print("[period] 2週間")


def _date_from_event_href(href: str) -> date | None:
    m = re.search(r"b(20\\d{6})", href or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


async def select_candidate_days(page: Page, cfg: dict, start: date) -> int:
    """Select day cells that have at least some availability (○/△) within horizon.

    The next screen contains time-slot detail. We deliberately include △ here,
    because a partially-open day may still have the requested 19:00-21:00 slot.
    """
    horizon = start + timedelta(days=int(cfg.get("days_ahead", 14)))
    targets: list[str] = []
    anchors = page.locator('a[href*="$dgTable$"]')
    # Older ASP.NET renders $ as encoded/plain depending on Playwright; fall back broadly.
    if not await anchors.count():
        anchors = page.locator('a[href*="dgTable"]')
    for i in range(await anchors.count()):
        a = anchors.nth(i)
        txt = normalize(await a.inner_text())
        if txt not in {"○", "△"}:
            continue
        href = (await a.get_attribute("href")) or ""
        d = _date_from_event_href(href)
        if d and start <= d <= horizon:
            targets.append(href)

    # De-duplicate while preserving order.
    targets = list(dict.fromkeys(targets))
    selected = 0
    for href in targets:
        loc = page.locator(f'a[href="{href}"]')
        if not await loc.count():
            continue
        try:
            await loc.first.click()
            await page.wait_for_timeout(120)
            selected += 1
        except Exception as e:
            print(f"[warning] day select failed: {href}: {e}")
    print(f"[candidate days] selected={selected}")
    return selected


async def scrape_time_detail(page: Page, cfg: dict) -> list[str]:
    """Conservative parser for the time-detail screen.

    Only reports rows/blocks where a concrete date and time range can be tied to
    an availability mark. This avoids false notifications from the legend text.
    """
    today = datetime.now().date()
    out: list[str] = []
    facility_keywords = cfg.get("facility_keywords", [])

    # Table rows first.
    rows = page.locator("tr")
    for i in range(await rows.count()):
        row = rows.nth(i)
        txt = normalize(await row.inner_text())
        if not txt or not any(t in txt for t in ["○", "△"]):
            continue
        if not _extract_time_ranges(txt):
            continue

        # Pull a little surrounding table context so a facility/date in a nearby
        # header can be associated with the slot row.
        table = row.locator("xpath=ancestor::table[1]")
        context = txt
        if await table.count():
            ttxt = normalize(await table.inner_text())
            if len(ttxt) <= 3000:
                context = ttxt
        if facility_keywords and not any(k in context for k in facility_keywords):
            # Some pages put facility in a preceding heading; use nearby parent text.
            parent = row.locator("xpath=ancestor::*[self::div or self::td][1]")
            if await parent.count():
                ptxt = normalize(await parent.inner_text())
                if len(ptxt) <= 3000:
                    context = ptxt + " | " + txt
        if not matches_requested_schedule(context, cfg, today=today):
            continue
        if facility_keywords and not any(k in context for k in facility_keywords):
            continue
        out.append(context)

    return sorted(set(out))

async def check_once(cfg: dict) -> list[str]:
    headless = os.getenv("HEADLESS", "1") != "0"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        page = await context.new_page()
        page.set_default_timeout(15000)

        await page.goto(BASE_URL, wait_until="networkidle")
        await inspect_page(page, "01_facility")
        await select_facilities(page, cfg.get("facility_keywords", []))
        await click_next(page)

        # Date-selection screen: request a 2-week grid to match days_ahead=14.
        await choose_two_week_view(page)
        await inspect_page(page, "02_after_facility")

        today = datetime.now().date()
        await set_date_if_possible(page, datetime.now())
        await click_next(page)
        await inspect_page(page, "03_results")

        # The facility result grid is only day-level. △ means some time slots are
        # available, so drill down into all ○/△ days in the configured horizon.
        selected = await select_candidate_days(page, cfg, today)
        if selected == 0:
            await browser.close()
            return []

        # Use the page footer's forward button, not an individual facility's
        # "次へ >>" link.
        forward = page.locator("#ucPCFooter_btnForward")
        if await forward.count():
            await forward.click()
            await page.wait_for_load_state("networkidle")
        else:
            await click_next(page)

        await inspect_page(page, "04_time_detail")
        rows = await scrape_time_detail(page, cfg)
        await browser.close()
        return rows


def fingerprint(row: str) -> str:
    return hashlib.sha256(row.encode("utf-8")).hexdigest()[:20]


async def main() -> None:
    cfg = load_config()
    rows = await check_once(cfg)
    current = {fingerprint(r): r for r in rows}
    seen = load_state()
    new_keys = set(current) - seen

    print(f"available={len(rows)} new={len(new_keys)}")
    for row in rows:
        print("-", row)

    if new_keys:
        lines = ["🎾 **町田市テニスコートに空きが見つかりました**", ""]
        for key in sorted(new_keys):
            lines.append(f"• {current[key]}")
        lines += ["", "予約サイト:", "https://www.pf489.com/machida/"]
        discord_notify(os.getenv("DISCORD_WEBHOOK_URL", ""), "\n".join(lines)[:1900])

    # Keep only currently available keys. If a slot fills and later reopens, it will notify again.
    save_state(set(current))


if __name__ == "__main__":
    asyncio.run(main())

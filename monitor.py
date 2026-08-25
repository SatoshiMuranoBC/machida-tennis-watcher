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
    """Select matching facility checkboxes/radios or links on the facility-selection page."""
    body = normalize(await page.locator("body").inner_text())
    missing = [k for k in facility_keywords if k not in body]
    if missing:
        print(f"[warning] facility keywords not visible on first page: {missing}")

    picked = 0
    # Prefer labels associated with checkboxes/radios.
    for keyword in facility_keywords:
        labels = page.locator("label", has_text=keyword)
        for i in range(await labels.count()):
            label = labels.nth(i)
            try:
                await label.click()
                picked += 1
            except Exception:
                pass

    # If no labels worked, click row-local checkbox/radio beside matching text.
    if picked == 0:
        for keyword in facility_keywords:
            textloc = page.get_by_text(keyword, exact=False)
            for i in range(await textloc.count()):
                el = textloc.nth(i)
                try:
                    row = el.locator("xpath=ancestor::tr[1]")
                    ctrl = row.locator("input[type=checkbox], input[type=radio]").first
                    if await ctrl.count():
                        await ctrl.check()
                        picked += 1
                        break
                except Exception:
                    pass

    # Some pages use a select list instead.
    if picked == 0:
        selects = page.locator("select")
        for i in range(await selects.count()):
            sel = selects.nth(i)
            opts = await sel.locator("option").all_text_contents()
            for keyword in facility_keywords:
                for opt in opts:
                    if keyword in normalize(opt):
                        try:
                            await sel.select_option(label=opt)
                            picked += 1
                        except Exception:
                            pass

    if picked == 0 and facility_keywords:
        raise RuntimeError("指定施設を選択できませんでした。施設名を config.yml で短めの部分一致にしてください。")


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


async def check_once(cfg: dict) -> list[str]:
    days_ahead = int(cfg.get("days_ahead", 14))
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
        await inspect_page(page, "02_after_facility")

        all_rows: list[str] = []
        today = datetime.now()
        # The site commonly shows a date range at once. We try the start date first, then parse all visible availability.
        await set_date_if_possible(page, today)
        try:
            await click_next(page)
        except Exception:
            # Some flows already display availability after facility selection.
            pass
        await inspect_page(page, "03_results")
        all_rows.extend(await scrape_available_rows(page, cfg))

        # Date filters are applied textually after scraping when possible.
        wanted_dates = {(today + timedelta(days=i)).strftime(fmt) for i in range(days_ahead + 1) for fmt in ["%-m/%-d", "%m/%d"]}
        filtered = [r for r in all_rows if any(d in r for d in wanted_dates)]
        if filtered:
            all_rows = filtered

        await browser.close()
        return sorted(set(all_rows))


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

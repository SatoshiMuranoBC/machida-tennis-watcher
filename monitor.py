from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib import request
import requests

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

    resp = requests.post(
        webhook,
        json={"content": message},
        headers={
            "User-Agent": "MachidaTennisWatcher/1.0",
            "Accept": "application/json",
        },
        timeout=20,
    )

    if resp.status_code >= 300:
        body = resp.text[:500]
        raise RuntimeError(
            f"Discord webhook failed: HTTP {resp.status_code} body={body}"
        )


STATE_FORMAT_VERSION = 2


def load_state() -> tuple[set[str], int]:
    if not STATE_FILE.exists():
        return set(), 0
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", [])), int(data.get("format_version", 1))
    except Exception:
        return set(), 0


def save_state(keys: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {
                "format_version": STATE_FORMAT_VERSION,
                "seen": sorted(keys),
                "updated_at": datetime.now().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
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



async def choose_month_view(page: Page) -> None:
    """Use the site's 1-month result grid for a 30-day monitoring horizon."""
    btn = page.locator("#rbtnMonth")
    if await btn.count():
        cls = (await btn.get_attribute("class")) or ""
        if "Orange" not in cls:
            await btn.click()
            await page.wait_for_load_state("networkidle")
            print("[period] 1ヶ月")


def _date_from_event_href(href: str) -> date | None:
    m = re.search(r"b(20\d{6})", href or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


async def collect_candidate_hrefs(page: Page, cfg: dict, start: date) -> list[str]:
    """Collect all ○/△ facility-date cells in the monitoring horizon.

    The Machida site allows at most 20 selected cells at once, so callers must
    process these hrefs in batches of 20 or fewer.
    """
    horizon = start + timedelta(days=int(cfg.get("days_ahead", 30)))
    targets: list[str] = []
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

    # Each href includes the facility ctlXX + date, so do NOT collapse by date.
    targets = list(dict.fromkeys(targets))
    print(f"[candidate cells] total={len(targets)} (site max selection=20)")
    return targets


async def select_candidate_batch(page: Page, hrefs: list[str]) -> int:
    """Select one batch of candidate facility-date cells (max 20)."""
    if len(hrefs) > 20:
        raise ValueError("candidate batch must be <= 20")

    selected = 0
    for href in hrefs:
        loc = page.locator(f'a[href="{href}"]')
        if not await loc.count():
            print(f"[warning] candidate disappeared before selection: {href}")
            continue
        try:
            await loc.first.click()
            await page.wait_for_timeout(120)
            selected += 1
        except Exception as e:
            print(f"[warning] day select failed: {href}: {e}")

    print(f"[candidate batch] requested={len(hrefs)} selected={selected}")
    return selected


def _slot_matches_schedule(slot_date: date, start_time: time, end_time: time, cfg: dict) -> bool:
    schedule = cfg.get("schedule", {})
    is_dayoff = slot_date.weekday() >= 5 or jpholiday.is_holiday(slot_date)

    if is_dayoff:
        return schedule.get("weekends_and_holidays", {}).get("all_day", True)

    for item in schedule.get("weekdays", {}).get("time_ranges", []):
        if start_time == _parse_hhmm(item["start"]) and end_time == _parse_hhmm(item["end"]):
            return True
    return False


def _slot_record(
    facility: str,
    slot_date: date,
    court: str,
    start_time: time,
    end_time: time,
    status: str,
) -> str:
    """Stable machine-readable representation used for state comparison."""
    return json.dumps(
        {
            "facility": facility,
            "date": slot_date.isoformat(),
            "court": court,
            "start": start_time.strftime("%H:%M"),
            "end": end_time.strftime("%H:%M"),
            "status": status,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_time_detail_text(raw_text: str, cfg: dict) -> list[str]:
    """Parse the time-detail page into one record per actually available court/time slot.

    The previous implementation returned a whole surrounding table as one result,
    which made notifications enormous and could mix dates together.  This parser
    treats each facility/date/court/time as an independent slot.
    """
    text = normalize(raw_text)
    facilities = list(dict.fromkeys(cfg.get("facility_keywords", [])))
    if not text or not facilities:
        return []

    facility_re = re.compile("|".join(sorted((re.escape(x) for x in facilities), key=len, reverse=True)))
    facility_matches = list(facility_re.finditer(text))
    if not facility_matches:
        return []

    # Machida's detail screen uses labels such as Ａ面, Ｂ面, ...
    court_re = re.compile(r"([Ａ-ＺA-Z０-９0-9一二三四五六七八九十]+面)\s+")
    results: list[str] = []
    today = datetime.now().date()

    for idx, fm in enumerate(facility_matches):
        block_end = facility_matches[idx + 1].start() if idx + 1 < len(facility_matches) else len(text)
        block = text[fm.start():block_end]
        facility = fm.group(0)
        slot_date = _extract_date(block, today)
        if slot_date is None:
            continue

        court_matches = list(court_re.finditer(block))
        if not court_matches:
            continue

        # Time headings are printed before the first court row, e.g.
        # 9:00～ 11:00 11:00～ 13:00 ...
        header = block[:court_matches[0].start()]
        time_ranges = _extract_time_ranges(header)
        if not time_ranges:
            continue

        for court_idx, cm in enumerate(court_matches):
            body_end = court_matches[court_idx + 1].start() if court_idx + 1 < len(court_matches) else len(block)
            body = block[cm.end():body_end]
            tokens = body.split()
            if not tokens:
                continue

            # First cell after the court name is the capacity column (often '－').
            # The next N cells correspond to the N time ranges in the header.
            statuses = tokens[1:1 + len(time_ranges)]
            if len(statuses) < len(time_ranges):
                print(
                    f"[warning] status columns short: {facility} {slot_date} {cm.group(1)} "
                    f"times={len(time_ranges)} statuses={len(statuses)}"
                )

            for (start_time, end_time), status in zip(time_ranges, statuses):
                if status not in {"○", "△"}:
                    continue
                if not _slot_matches_schedule(slot_date, start_time, end_time, cfg):
                    continue
                results.append(
                    _slot_record(
                        facility,
                        slot_date,
                        cm.group(1),
                        start_time,
                        end_time,
                        status,
                    )
                )

    return sorted(set(results))


async def scrape_time_detail(page: Page, cfg: dict) -> list[str]:
    raw_text = await page.locator("body").inner_text()
    return _parse_time_detail_text(raw_text, cfg)


def _format_slot_group(records: list[dict]) -> list[str]:
    weekdays = "月火水木金土日"
    lines: list[str] = []

    # Group by facility + date while keeping chronological order.
    records = sorted(records, key=lambda r: (r["date"], r["facility"], r["start"], r["court"]))
    current_key = None
    for r in records:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        key = (r["facility"], r["date"])
        if key != current_key:
            if lines:
                lines.append("")
            lines.append(f'**{r["facility"]}**')
            lines.append(f'📅 {d.month}/{d.day}（{weekdays[d.weekday()]}）')
            current_key = key

        status_label = "空き" if r.get("status") == "○" else "条件付き"
        lines.append(f'・{r["court"]}　{r["start"]}〜{r["end"]}　{r["status"]} {status_label}')

    return lines


def _build_notification_messages(records: list[dict]) -> list[str]:
    """Build Discord messages without exceeding Discord's 2000-character limit."""
    header = f"🎾 **町田市テニスコート 空き通知**\n新しい空き：{len(records)}件\n"
    footer = "\n🔗 [予約サイトを開く](https://www.pf489.com/machida/)"
    body_lines = _format_slot_group(records)

    messages: list[str] = []
    current = header
    for line in body_lines:
        addition = line + "\n"
        if len(current) + len(addition) + len(footer) > 1900:
            messages.append(current.rstrip() + footer)
            current = "🎾 **空き通知（続き）**\n" + addition
        else:
            current += addition
    messages.append(current.rstrip() + footer)
    return messages

async def _open_results_page(page: Page, cfg: dict) -> None:
    """Navigate from the tennis entry page to the 1-month facility result grid."""
    await page.goto(BASE_URL, wait_until="networkidle")
    await select_facilities(page, cfg.get("facility_keywords", []))
    await click_next(page)

    await choose_month_view(page)
    await set_date_if_possible(page, datetime.now())
    await click_next(page)


async def check_once(cfg: dict) -> list[str]:
    headless = os.getenv("HEADLESS", "1") != "0"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        # First pass: discover every ○/△ facility-date cell.
        context = await browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        page = await context.new_page()
        page.set_default_timeout(15000)

        await page.goto(BASE_URL, wait_until="networkidle")
        await inspect_page(page, "01_facility")
        await select_facilities(page, cfg.get("facility_keywords", []))
        await click_next(page)
        await choose_month_view(page)
        await inspect_page(page, "02_after_facility")

        today = datetime.now().date()
        await set_date_if_possible(page, datetime.now())
        await click_next(page)
        await inspect_page(page, "03_results")

        targets = await collect_candidate_hrefs(page, cfg, today)
        await context.close()

        if not targets:
            await browser.close()
            print("[result] no ○/△ candidate cells")
            return []

        # IMPORTANT: the Machida site caps selection at 20 cells.
        # Process all candidates in chunks so later facilities (especially
        # 野津田公園北) are not silently omitted.
        all_rows: list[str] = []
        batch_size = 20
        batches = [targets[i:i + batch_size] for i in range(0, len(targets), batch_size)]
        print(f"[candidate batches] count={len(batches)}")

        for batch_no, batch in enumerate(batches, 1):
            context = await browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
            page = await context.new_page()
            page.set_default_timeout(15000)

            await _open_results_page(page, cfg)
            selected = await select_candidate_batch(page, batch)
            if selected == 0:
                await context.close()
                continue

            forward = page.locator("#ucPCFooter_btnForward")
            if await forward.count():
                await forward.click()
                await page.wait_for_load_state("networkidle")
            else:
                await click_next(page)

            # Keep per-batch diagnostics if a run has to be inspected later.
            await inspect_page(page, f"04_time_detail_batch{batch_no}")
            rows = await scrape_time_detail(page, cfg)
            print(f"[batch {batch_no}] matched_rows={len(rows)}")
            for r in rows:
                print(f"[matched] {r[:800]}")
            all_rows.extend(rows)

            await context.close()

        await browser.close()
        deduped = sorted(set(all_rows))
        print(f"[result] matched_total={len(deduped)}")
        return deduped


def fingerprint(row: str) -> str:
    return hashlib.sha256(row.encode("utf-8")).hexdigest()[:20]


async def main() -> None:
    cfg = load_config()

    # Manual notification test. This is only triggered when the workflow
    # explicitly sets TEST_NOTIFICATION=1.
    if os.getenv("TEST_NOTIFICATION", "0") == "1":
        discord_notify(
            os.getenv("DISCORD_WEBHOOK_URL", ""),
            "🎾 **町田市テニス空き監視：テスト通知**\n"
            "GitHub Actions からDiscordへの通知に成功しました。"
        )
        print("[test] Discord test notification sent")
        return

    rows = await check_once(cfg)
    current = {fingerprint(r): r for r in rows}
    seen, state_version = load_state()

    # v2 changes state granularity from a whole page/table to one individual slot.
    # When upgrading an existing installation, silently establish a new baseline once
    # instead of sending a large one-off notification for every currently open slot.
    if state_version not in {0, STATE_FORMAT_VERSION}:
        print(f"[state] migrating format v{state_version} -> v{STATE_FORMAT_VERSION}; baseline reset")
        new_keys: set[str] = set()
    else:
        new_keys = set(current) - seen

    print(f"available={len(rows)} new={len(new_keys)}")
    for row in rows:
        try:
            r = json.loads(row)
            print(f'- {r["facility"]} {r["date"]} {r["court"]} {r["start"]}-{r["end"]} {r["status"]}')
        except Exception:
            print("-", row)

    if new_keys:
        new_records = [json.loads(current[key]) for key in sorted(new_keys)]
        for message in _build_notification_messages(new_records):
            discord_notify(os.getenv("DISCORD_WEBHOOK_URL", ""), message)

    # Keep only currently available keys. If a slot fills and later reopens, it will notify again.
    save_state(set(current))


if __name__ == "__main__":
    asyncio.run(main())

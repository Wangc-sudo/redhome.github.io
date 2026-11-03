"""可见 Chrome + CDP 的单轮猫超采集器（无独立循环、无钉钉直发）。

只保留主体切换、100 条/页、完整翻页、市场价字段与恢复主体。内部最低价和
自动反馈不进入正式链路。Cookie 始终留在原 Chrome Profile。
"""
from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from windows_agent.maochao.auth import LOGIN_REQUIRED, VALID, LoginRequiredError, classify_auth_page

BEIJING = timezone(timedelta(hours=8))
TARGET_FRAGMENT = "ProductPriceForce"
DEFAULT_PAGE_URL = (
    "https://web.txcs.tmall.com/?frameUrl="
    "https%3A%2F%2Ftbmc.portal.tmall.com%2Fmmc-ice3-app%2Fmmc-price-ice3%2F"
    "ProductPriceForce%3FbizTypeShop%3DMAOCHAO"
)
CSV_FIELDS = (
    "business_date", "subject_key", "item_id", "sku_id", "product", "product_spec",
    "current_price", "market_price", "suggested_price", "price_gap", "page_status",
    "market_source", "market_product", "market_spec", "page_number", "row_index",
    "captured_at",
)


class CaptureFailedError(RuntimeError):
    pass


def number_or_none(text):
    match = re.search(r"(?<!\d)(\d+(?:\.\d{1,2})?)(?!\d)", (text or "").replace(",", ""))
    return float(match.group(1)) if match else None


def current_subject(page):
    match = re.search(r"Hi,\s*([^\n]+)", page.locator("body").inner_text())
    return match.group(1).strip() if match else None


def discover_subjects(page):
    subjects = []
    for line in page.locator("body").inner_text().splitlines():
        value = line.strip()
        if re.match(r"^\d{6,}-", value) and value not in subjects:
            subjects.append(value)
    return subjects


def enrich_market_comparison(row):
    cells = row.pop("cells", [])
    row.pop("links", None)
    market_text = cells[2] if len(cells) > 2 else ""
    lines = [line.strip() for line in market_text.splitlines() if line.strip()]
    row["market_price"] = number_or_none(lines[0] if lines else "")
    detail = lines[1] if len(lines) > 1 else ""
    row["market_product"], _, row["market_spec"] = detail.partition("/")
    row["market_source"] = next(
        (source for source in ("京东", "拼多多", "抖音", "天猫", "淘宝", "猫超", "苏宁", "唯品会")
         if source in market_text),
        "未标注",
    )
    current = row.get("current_price")
    market = row.get("market_price")
    row["price_gap"] = round(current - market, 2) if current is not None and market is not None else None


def extract_page_rows(frame, page_number):
    dates = frame.locator("input").evaluate_all(
        "els => els.map(e => e.value || '').filter(v => /^\\d{4}-\\d{2}-\\d{2}$/.test(v))"
    )
    raw_rows = frame.locator("table tbody tr, .next-table-row").evaluate_all(
        "rows => rows.map((r,index) => {const cells = Array.from(r.querySelectorAll('th,td,.next-table-cell')).map(c => (c.innerText || '').trim()); const text = (r.innerText || '').trim(); const links = Array.from(r.querySelectorAll('a')).map(a => ({text:(a.innerText||'').trim(),href:a.href})); return {rowIndex:index,cells,text,links}}).filter(r => r.text)"
    )
    rows = []
    seen = set()
    for raw in raw_rows:
        cells, text = raw["cells"], raw["text"]
        if not cells or any(marker in text for marker in ("共 0 条数据", "共 0 项数据", "没有数据", "暂无数据")):
            continue
        fingerprint = json.dumps(cells, ensure_ascii=False)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        item_match = re.search(r"(?:ItemID|商品ID|商品编码)\s*[:：]?\s*(\d{5,})", text, re.I)
        if not item_match:
            href_text = " ".join(link["href"] for link in raw["links"])
            item_match = re.search(r"(?:[?&](?:id|itemId|item_id)=)(\d{5,})", href_text, re.I)
        sku_match = re.search(r"(?:SKU(?:ID)?|商品SKU)\s*[:：]?\s*([A-Za-z0-9_-]+)", text, re.I)
        rows.append({
            "page_number": page_number,
            "row_index": raw["rowIndex"] + 1,
            "item_id": item_match.group(1) if item_match else "",
            "sku_id": sku_match.group(1) if sku_match else "",
            "product": cells[0].splitlines()[0] if cells else "",
            "product_spec": cells[1].strip() if len(cells) > 1 else "",
            "current_price": number_or_none(cells[3]) if len(cells) > 3 else None,
            "suggested_price": number_or_none(cells[4]) if len(cells) > 4 else None,
            "page_status": cells[6] if len(cells) > 6 else "",
            "cells": cells,
            "links": raw["links"],
        })
    return rows, dates


def set_page_size_100(frame):
    selector = frame.locator(".next-pagination-size-selector-dropdown")
    if not selector.count():
        text = frame.locator("body").inner_text()
        if "共 0 条数据" in text or "暂无数据" in text:
            return False
        raise CaptureFailedError("page_size_selector_missing")
    current = selector.locator("em").first.get_attribute("title")
    if current == "100":
        return True
    selector.click()
    frame.page.wait_for_timeout(300)
    options = frame.locator("li[role='option'], .next-select-menu-item")
    clicked = False
    for index in range(options.count()):
        if options.nth(index).is_visible() and options.nth(index).inner_text().strip() == "100":
            options.nth(index).click()
            clicked = True
            break
    if not clicked:
        raise CaptureFailedError("page_size_option_missing")
    frame.page.wait_for_timeout(2000)
    if selector.locator("em").first.get_attribute("title") != "100":
        raise CaptureFailedError("page_size_verify_failed")
    return True


def current_page_number(frame):
    current = frame.locator(".next-pagination .next-current")
    return int(current.inner_text().strip()) if current.count() else 1


def go_to_next_page(frame):
    button = frame.locator(".next-pagination .next-next")
    if not button.count() or button.is_disabled():
        return False
    before = current_page_number(frame)
    button.click()
    frame.page.wait_for_timeout(2000)
    after = current_page_number(frame)
    if after == before:
        frame.page.wait_for_timeout(2500)
        after = current_page_number(frame)
    if after == before:
        raise CaptureFailedError("page_did_not_advance")
    return True


class MaochaoCollector:
    def __init__(self, *, cdp_url="http://127.0.0.1:9222", output_dir=r"C:\PriceAgent\data\inspection",
                 page_url=DEFAULT_PAGE_URL):
        self.cdp_url = cdp_url
        self.output_dir = Path(output_dir)
        self.page_url = page_url

    def _find_page(self, browser):
        pages = [page for context in browser.contexts for page in context.pages]
        page = next(
            (item for item in pages if TARGET_FRAGMENT in item.url or any(
                TARGET_FRAGMENT in frame.url for frame in item.frames
            )),
            None,
        )
        if page is None:
            raise LoginRequiredError("price_page_not_found")
        return page

    def _find_frame(self, page):
        frame = next(
            (item for item in page.frames if item != page.main_frame and (
                item.url.startswith("https://tbmc.portal.tmall.com/") and TARGET_FRAGMENT in item.url
            )),
            None,
        )
        if frame is None:
            frame = next(
                (item for item in page.frames if item != page.main_frame and
                 "商品编码" in item.locator("body").inner_text() and
                 "建议价" in item.locator("body").inner_text()),
                None,
            )
        if frame is None:
            state = classify_auth_page(page.url, page.locator("body").inner_text(), False)
            if state == LOGIN_REQUIRED:
                raise LoginRequiredError("login_required")
            raise CaptureFailedError("price_frame_not_found")
        return frame

    def _switch_subject(self, page, target, timeout_ms):
        if current_subject(page) == target:
            return
        for attempt in range(2):
            page.keyboard.press("Escape")
            opener = page.locator(".current-supplier-name")
            opened = False
            for index in range(opener.count()):
                if opener.nth(index).is_visible():
                    opener.nth(index).click()
                    opened = True
                    break
            if opened:
                break
            if attempt == 0:
                page.goto(self.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(8000)
        if not opened:
            raise CaptureFailedError("subject_menu_missing")
        page.wait_for_timeout(800)
        option = page.get_by_text(target, exact=True)
        for index in range(option.count()):
            if option.nth(index).is_visible():
                option.nth(index).click()
                page.wait_for_timeout(5000)
                return
        raise CaptureFailedError("subject_option_missing")

    def _capture_subject(self, page, subject, config, progress):
        timeout_ms = config["page_ready_timeout_seconds"] * 1000
        page.goto(self.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(8000)
        frame = self._find_frame(page)
        if current_subject(page) != subject:
            raise CaptureFailedError("subject_mismatch")
        page_size_100 = set_page_size_100(frame)
        rows, pages = [], 0
        complete = False
        business_date = None
        for _ in range(100):
            number = current_page_number(frame)
            progress(subject_key=subject.split("-", 1)[0], current_page=number, auth_state=VALID)
            page_rows, dates = extract_page_rows(frame, number)
            if business_date is None and dates:
                business_date = dates[0]
            for row in page_rows:
                enrich_market_comparison(row)
                row.update(
                    subject_key=subject.split("-", 1)[0],
                    business_date=business_date or datetime.now(BEIJING).date().isoformat(),
                )
            rows.extend(page_rows)
            pages += 1
            if not go_to_next_page(frame):
                complete = True
                break
        if not complete:
            raise CaptureFailedError("page_limit_exceeded")
        return rows, {
            "subject_key": subject.split("-", 1)[0],
            "status": "completed",
            "row_count": len(rows),
            "pages_captured": pages,
            "page_size_100": page_size_100,
            "empty": not rows,
        }

    def capture(self, job_id, config, *, progress=None):
        progress = progress or (lambda **kwargs: None)
        captured_at = datetime.now(BEIJING)
        timeout_ms = config["page_ready_timeout_seconds"] * 1000
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.cdp_url, timeout=15000)
            page = self._find_page(browser)
            page.bring_to_front()
            discovered = discover_subjects(page)
            by_id = {value.split("-", 1)[0]: value for value in discovered}
            missing = [key for key in config["subjects"] if key not in by_id]
            if missing:
                raise CaptureFailedError("configured_subject_missing")
            rows, summaries = [], []
            for key in config["subjects"]:
                subject = by_id[key]
                for attempt in range(config["subject_attempts"]):
                    try:
                        self._switch_subject(page, subject, timeout_ms)
                        subject_rows, summary = self._capture_subject(page, subject, config, progress)
                        rows.extend(subject_rows)
                        summaries.append(summary)
                        break
                    except LoginRequiredError:
                        raise
                    except Exception as exc:
                        if attempt + 1 >= config["subject_attempts"]:
                            raise CaptureFailedError(f"subject_failed:{key}") from exc
                        page.wait_for_timeout(2000)
            restore = by_id[config["restore_subject"]]
            self._switch_subject(page, restore, timeout_ms)
            page.goto(self.page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            if current_subject(page) != restore:
                raise CaptureFailedError("restore_subject_failed")
            self._find_frame(page)
            for row in rows:
                row["captured_at"] = captured_at.isoformat()
            payload = {
                "complete": True,
                "restored_subject": config["restore_subject"],
                "subjects": summaries,
                "rows": rows,
            }
            directory = self.output_dir / job_id
            directory.mkdir(parents=True, exist_ok=True)
            screenshot = directory / f"{job_id}_page.png"
            page.screenshot(path=str(screenshot), full_page=False)
            json_path = directory / f"{job_id}_result.json"
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            csv_path = directory / f"{job_id}_comparison.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            local_artifacts = [
                {"artifact_type": "page_screenshot", "path": screenshot},
                {"artifact_type": "json", "path": json_path},
                {"artifact_type": "csv", "path": csv_path},
            ]
            progress(auth_state=VALID)
            return payload, local_artifacts, captured_at

    def wait_for_login_recovery(self, config, *, progress=None, should_stop=None):
        """不刷新、不抢焦点，等待运维在同一可见 Chrome 完成人工登录。"""
        progress = progress or (lambda **kwargs: None)
        should_stop = should_stop or (lambda: False)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.cdp_url, timeout=15000)
            while True:
                if should_stop():
                    raise LoginRequiredError("login_expired")
                progress(auth_state=LOGIN_REQUIRED)
                try:
                    page = self._find_page(browser)
                    self._find_frame(page)
                    progress(auth_state=VALID)
                    return
                except LoginRequiredError:
                    pass
                except Exception:
                    # 人工登录导航期间 DOM 短暂不可用；继续心跳与等待。
                    pass
                page = next(
                    (item for context in browser.contexts for item in context.pages),
                    None,
                )
                if page is not None:
                    page.wait_for_timeout(config["auth_poll_seconds"] * 1000)
                else:
                    time.sleep(config["auth_poll_seconds"])

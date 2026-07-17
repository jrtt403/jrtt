#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError as exc:
    raise SystemExit(
        "Playwright is not installed. Run: python3 -m pip install playwright && "
        "python3 -m playwright install chromium"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / "jrtt"
DATA_DIR = ROOT / "data"
ARTICLES_DIR = ROOT / "articles"
DB_FILE = DATA_DIR / "jrtt.db"
DEFAULT_PROFILE_DIR = DATA_DIR / "toutiao_profile"
DEFAULT_OUTPUT = DATA_DIR / "toutiao_metrics_auto.csv"
DEFAULT_SCREENSHOT = DATA_DIR / "toutiao_metrics_last.png"
CONTENT_URL = "https://mp.toutiao.com/profile_v4/manage/content/all"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from metrics import import_metrics_csv  # noqa: E402


def is_login_page(page) -> bool:
    current = page.url.lower()
    if any(marker in current for marker in ("login", "passport", "sso")):
        return True
    try:
        return bool(page.get_by_text("登录", exact=True).count()) and not bool(
            page.get_by_text("作品管理").count()
        )
    except PlaywrightError:
        return False


def ensure_logged_in(page, login_wait_seconds: int) -> None:
    if not is_login_page(page):
        return
    if login_wait_seconds <= 0:
        raise RuntimeError("Toutiao redirected to login. Re-run with --login-wait-seconds and log in.")

    print(
        f"Toutiao login required. Complete login in the opened browser within "
        f"{login_wait_seconds}s...",
        file=sys.stderr,
    )
    deadline = time.time() + login_wait_seconds
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        if not is_login_page(page):
            page.goto(CONTENT_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            if not is_login_page(page):
                return
    raise RuntimeError("Toutiao login timed out.")


def launch_context(playwright, profile_dir: Path, headless: bool):
    launch_kwargs = {
        "headless": headless,
        "viewport": {"width": 1440, "height": 1200},
        "locale": "zh-CN",
    }
    try:
        return playwright.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            **launch_kwargs,
        )
    except PlaywrightError:
        return playwright.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)


def wait_for_feed_response(page, timeout_ms: int, login_wait_seconds: int) -> tuple[str, dict]:
    captured: list[tuple[str, dict]] = []

    def on_response(response) -> None:
        url = response.url
        if "api/feed/mp_provider/v1" not in url:
            return
        try:
            captured.append((url, response.json()))
        except PlaywrightError:
            return

    page.on("response", on_response)
    try:
        page.goto(CONTENT_URL, wait_until="domcontentloaded", timeout=60000)
        ensure_logged_in(page, login_wait_seconds)
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if captured:
                return captured[-1]
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("response", on_response)
    raise RuntimeError("Toutiao content feed API was not detected.")


def fetch_feed_page(page, url: str) -> dict:
    return page.evaluate(
        """
        async (url) => {
            const response = await fetch(url, { credentials: 'include' });
            const text = await response.text();
            try {
                return JSON.parse(text);
            } catch (error) {
                return { errno: -1, message: text.slice(0, 500) };
            }
        }
        """,
        url,
    )


def next_feed_url(url: str, offset: object, page_index: int) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    query["offset"] = [str(offset)]
    client_extra = {}
    try:
        client_extra = json.loads(query.get("client_extra_params", ["{}"])[0])
    except json.JSONDecodeError:
        client_extra = {}
    client_extra["page_index"] = str(page_index)
    query["client_extra_params"] = [json.dumps(client_extra, ensure_ascii=False, separators=(",", ":"))]
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urllib.parse.urlencode(query, doseq=True),
            parts.fragment,
        )
    )


def collect_content_items(page, first_url: str, first_data: dict, max_items: int) -> list[dict]:
    items: list[dict] = []
    data = first_data
    page_index = 1
    count = feed_count(first_url)

    while True:
        items.extend(extract_feed_items(data))
        if len(items) >= max_items:
            return items[:max_items]
        if not data.get("has_more"):
            return items

        page_index += 1
        url = next_feed_url(first_url, (page_index - 1) * count, page_index)
        data = fetch_feed_page(page, url)
        if not isinstance(data, dict) or "data" not in data:
            return items
        page.wait_for_timeout(400)


def feed_count(url: str) -> int:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    try:
        return max(1, int(query.get("count", ["10"])[0]))
    except ValueError:
        return 10


def extract_feed_items(data: dict) -> list[dict]:
    rows = []
    for raw in data.get("data") or []:
        cell = raw.get("assembleCell", {}).get("itemCell", {}) if isinstance(raw, dict) else {}
        base = cell.get("articleBase") or {}
        counters = cell.get("itemCounter") or {}
        if not base.get("title"):
            continue
        rows.append(
            {
                "title": str(base.get("title") or "").strip(),
                "content_id": str(base.get("gidStr") or base.get("groupID") or base.get("itemID") or ""),
                "publish_time": parse_publish_time(base.get("publishTime") or base.get("createTime")),
                "url": clean_toutiao_item_url(str(base.get("articleURL") or base.get("schema") or "")),
                "status": parse_status(cell),
                "impressions": parse_int(counters.get("showCount")),
                "reads": parse_int(counters.get("readCount") or counters.get("videoWatchCount")),
                "likes": parse_int(counters.get("diggCount")),
                "comments": parse_int(counters.get("commentCount")),
                "favorites": parse_int(counters.get("repinCount")),
                "shares": parse_int(counters.get("shareCount")),
            }
        )
    return rows


def parse_status(cell: dict) -> str:
    review = cell.get("reviewInfo") or {}
    title = str(review.get("title") or "").strip()
    if title:
        return title
    status = review.get("status")
    if status == 3:
        return "已发布"
    if status is None:
        return ""
    return str(status)


def parse_publish_time(value: object) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def clean_toutiao_item_url(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def parse_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace(",", "").strip()
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else 0


def click_rate(reads: int, impressions: int) -> str:
    if impressions <= 0:
        return ""
    return f"{reads / impressions:.6f}"


def write_metrics_csv(items: list[dict], output: Path, metric_date: str) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "标题",
        "日期",
        "发布时间",
        "作品ID",
        "状态",
        "展现量",
        "阅读量",
        "点击率",
        "完读率",
        "平均阅读时长",
        "点赞",
        "评论",
        "收藏",
        "分享",
        "链接",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    "标题": item["title"],
                    "日期": metric_date,
                    "发布时间": item["publish_time"],
                    "作品ID": item["content_id"],
                    "状态": item["status"],
                    "展现量": item["impressions"],
                    "阅读量": item["reads"],
                    "点击率": click_rate(item["reads"], item["impressions"]),
                    "完读率": "",
                    "平均阅读时长": "",
                    "点赞": item["likes"],
                    "评论": item["comments"],
                    "收藏": item["favorites"],
                    "分享": item["shares"],
                    "链接": item["url"],
                }
            )
    return output


def import_csv_to_db(csv_path: Path, metric_date: str | None) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    result = import_metrics_csv(
        conn,
        ARTICLES_DIR,
        csv_path,
        default_date=metric_date,
        publish_events_path=DATA_DIR / "toutiao_publish_events.jsonl",
    )
    print(
        f"imported={result.imported} skipped={result.skipped} "
        f"unmatched={len(result.unmatched)} db={DB_FILE}"
    )
    if result.unmatched:
        print("unmatched titles:", file=sys.stderr)
        for title in result.unmatched[:10]:
            print(f"- {title}", file=sys.stderr)


def fetch_metrics(args: argparse.Namespace) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    metric_date = args.date or dt.datetime.now().astimezone().date().isoformat()

    with sync_playwright() as playwright:
        context = launch_context(playwright, args.profile_dir, args.headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            first_url, first_data = wait_for_feed_response(
                page,
                args.timeout_ms,
                args.login_wait_seconds,
            )
            items = collect_content_items(page, first_url, first_data, args.max_items)
            if args.published_only:
                items = [item for item in items if item["status"] in {"已发布", "3", ""}]
            output = write_metrics_csv(items, args.output, metric_date)
            print(f"items={len(items)}")
            print(f"csv={output}")
            if items:
                total_impressions = sum(item["impressions"] for item in items)
                total_reads = sum(item["reads"] for item in items)
                print(f"impressions={total_impressions} reads={total_reads}")
            if args.import_db:
                import_csv_to_db(output, metric_date)
        except Exception:
            try:
                page.screenshot(path=str(args.screenshot), full_page=True)
                print(f"screenshot={args.screenshot}", file=sys.stderr)
            except Exception as screenshot_error:
                print(f"failed to save screenshot: {screenshot_error}", file=sys.stderr)
            raise
        finally:
            context.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Toutiao content metrics with Playwright.")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
    parser.add_argument("--date", help="metric date, defaults to today")
    parser.add_argument("--max-items", type=int, default=120)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--headless", action="store_true", help="run without visible browser UI")
    parser.add_argument("--import-db", action="store_true", help="import CSV into local metrics DB")
    parser.add_argument("--include-unpublished", dest="published_only", action="store_false")
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=0,
        help="when logged out in headed mode, wait this long for manual login",
    )
    parser.set_defaults(published_only=True)
    return parser.parse_args()


def main() -> None:
    fetch_metrics(parse_args())


if __name__ == "__main__":
    main()

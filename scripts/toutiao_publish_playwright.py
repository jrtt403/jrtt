#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
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
ARTICLES_DIR = ROOT / "articles"
DATA_DIR = ROOT / "data"
DEFAULT_PROFILE_DIR = DATA_DIR / "toutiao_profile"
DEFAULT_SCREENSHOT = DATA_DIR / "toutiao_last.png"
DEFAULT_PUBLISH_EVENTS = DATA_DIR / "toutiao_publish_events.jsonl"
PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"


def latest_article_path() -> Path:
    articles = sorted(ARTICLES_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not articles:
        raise SystemExit(f"no articles found in {ARTICLES_DIR}")
    return articles[0]


def article_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def clean_article_body(body: str) -> str:
    body = re.sub(r"^#{1,6}\s*", "", body, flags=re.MULTILINE)
    body = re.sub(r"\*\*([^*]+)\*\*", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def load_article(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    if "## 文章" in text:
        text = text.split("## 文章", 1)[1]
    if "## 发布前人工检查" in text:
        text = text.split("## 发布前人工检查", 1)[0]
    text = text.strip()
    lines = text.splitlines()
    if not lines:
        raise SystemExit(f"empty article: {path}")
    title = lines[0].removeprefix("#").strip()
    body = clean_article_body("\n".join(lines[1:]))
    if not title or not body:
        raise SystemExit(f"failed to parse article title/body: {path}")
    return title, body


def first_visible(page, selectors: list[str], timeout_ms: int = 8000):
    last_error: Exception | None = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise PlaywrightTimeoutError(f"none of selectors became visible: {selectors}") from last_error


def click_text(page, text: str, exact: bool = True, timeout_ms: int = 8000) -> None:
    locator = page.get_by_text(text, exact=exact).first
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.click()


def fill_editor(page, title: str, body: str) -> None:
    title_box = first_visible(
        page,
        [
            "textarea[placeholder*='文章标题']",
            "[contenteditable='true'][placeholder*='文章标题']",
            "[contenteditable='true']:has-text('请输入文章标题')",
        ],
    )
    fill_textbox(page, title_box, title)

    body_box = first_visible(
        page,
        [
            "textarea[placeholder*='正文']",
            "[contenteditable='true'][placeholder*='正文']",
            "[contenteditable='true']:has-text('请输入正文')",
            ".ProseMirror",
            "[role='textbox']",
        ],
        timeout_ms=12000,
    )
    fill_textbox(page, body_box, body)


def fill_textbox(page, locator, value: str) -> None:
    locator.click()
    try:
        locator.fill(value, timeout=5000)
    except PlaywrightError:
        page.keyboard.press("Meta+A")
        page.keyboard.type(value)


def ensure_logged_in(page, login_wait_seconds: int = 0) -> None:
    if not is_login_page(page):
        return
    if login_wait_seconds > 0:
        print(
            f"Toutiao login required. Complete login in the opened browser within "
            f"{login_wait_seconds}s...",
            file=sys.stderr,
        )
        deadline = time.time() + login_wait_seconds
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            if not is_login_page(page):
                page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                if not is_login_page(page):
                    return
        raise RuntimeError("Toutiao login timed out.")
    raise RuntimeError("Toutiao redirected to login. Re-run with --login-wait-seconds and log in.")


def is_login_page(page) -> bool:
    current = page.url
    login_markers = ["login", "passport", "sso"]
    if any(marker in current.lower() for marker in login_markers):
        return True
    if page.get_by_text("登录", exact=True).count() and not page.get_by_text("预览并发布").count():
        return True
    return False


def maybe_click(page, text: str, timeout_ms: int = 2500) -> bool:
    try:
        click_text(page, text, timeout_ms=timeout_ms)
        return True
    except PlaywrightError:
        return False


def wait_for_draft_saved(page, timeout_s: int = 20) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.get_by_text("草稿已保存").count() or page.get_by_text("草稿将自动保存").count():
            return
        page.wait_for_timeout(500)


def record_publish_event(
    article_path: Path,
    title: str,
    status: str,
    success_marker_detected: bool,
    events_path: Path,
) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        article_path_rel = article_path.relative_to(ROOT)
    except ValueError:
        article_path_rel = article_path
    event = {
        "article_path": str(article_path.resolve()),
        "article_path_rel": str(article_path_rel),
        "title": title,
        "published_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "success_marker_detected": success_marker_detected,
    }
    with events_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def publish_to_toutiao(args: argparse.Namespace) -> None:
    article_path = latest_article_path() if args.article == "latest" else Path(args.article)
    if not article_path.is_absolute():
        article_path = ROOT / article_path
    title, body = load_article(article_path)
    body_chars = article_char_count(body)
    if body_chars < args.min_chars:
        raise SystemExit(f"article body too short: {body_chars} < {args.min_chars}")

    DATA_DIR.mkdir(exist_ok=True)
    args.profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": args.headless,
            "viewport": {"width": 1440, "height": 1200},
            "locale": "zh-CN",
        }
        try:
            context = p.chromium.launch_persistent_context(
                str(args.profile_dir),
                channel="chrome",
                **launch_kwargs,
            )
        except PlaywrightError:
            context = p.chromium.launch_persistent_context(str(args.profile_dir), **launch_kwargs)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            ensure_logged_in(page, args.login_wait_seconds)

            fill_editor(page, title, body)
            wait_for_draft_saved(page)

            maybe_click(page, "无封面")
            if args.no_ads:
                maybe_click(page, "不投放广告")
            if args.declare_ai:
                maybe_click(page, "引用AI")
            wait_for_draft_saved(page)

            click_text(page, "预览并发布", exact=True, timeout_ms=15000)
            page.wait_for_timeout(3000)

            status = "previewed"
            success_marker_detected = False
            if args.confirm_publish:
                click_text(page, "确认发布", exact=True, timeout_ms=15000)
                page.wait_for_timeout(5000)
                success_marker_detected = bool(
                    page.get_by_text("提交成功").count()
                    or page.get_by_text("发布成功").count()
                    or page.get_by_text("审核").count()
                )
                status = "submitted"
                record_publish_event(
                    article_path,
                    title,
                    status,
                    success_marker_detected,
                    args.publish_events,
                )
                if not success_marker_detected:
                    print("publish clicked, but no success marker detected yet", file=sys.stderr)
            else:
                print("preview opened; pass --confirm-publish to click final publish", file=sys.stderr)

            page.screenshot(path=str(args.screenshot), full_page=True)
            print(f"title={title}")
            print(f"body_chars={body_chars}")
            print(f"screenshot={args.screenshot}")
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
    parser = argparse.ArgumentParser(description="Publish latest generated article to Toutiao.")
    parser.add_argument("--article", default="latest", help="article markdown path or 'latest'")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
    parser.add_argument("--publish-events", type=Path, default=DEFAULT_PUBLISH_EVENTS)
    parser.add_argument("--headless", action="store_true", help="run without visible browser UI")
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=0,
        help="when logged out in headed mode, wait this long for manual login",
    )
    parser.add_argument(
        "--confirm-publish",
        action="store_true",
        help="click the final Toutiao confirm-publish button",
    )
    parser.add_argument("--min-chars", type=int, default=1000)
    parser.add_argument("--no-ads", action="store_true", help="select no ads before publishing")
    parser.add_argument(
        "--no-declare-ai",
        dest="declare_ai",
        action="store_false",
        help="do not select the AI citation declaration",
    )
    parser.set_defaults(declare_ai=True)
    return parser.parse_args()


def main() -> None:
    publish_to_toutiao(parse_args())


if __name__ == "__main__":
    main()

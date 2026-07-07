from __future__ import annotations

import html
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class ArticleContext:
    requested_url: str
    final_url: str
    title: str
    text: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text)

    def excerpt(self, limit: int = 5000) -> str:
        return self.text[:limit].strip()


class ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._tag_stack: list[str] = []
        self._capture_title = False
        self._capture_text = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._tag_stack.append(tag)
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._capture_title = True
        if tag in {"h1", "h2", "h3", "p", "li"}:
            self._capture_text = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._capture_title = False
        if tag in {"h1", "h2", "h3", "p", "li"}:
            self._capture_text = False
            self.text_parts.append("\n")
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = normalize_space(data)
        if not value:
            return
        if self._capture_title:
            self.title_parts.append(value)
        if self._capture_text:
            self.text_parts.append(value)

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))

    @property
    def text(self) -> str:
        lines = []
        for line in "\n".join(self.text_parts).splitlines():
            line = normalize_space(line)
            if len(line) >= 20 and not looks_like_navigation(line):
                lines.append(line)
        return "\n".join(dedupe_keep_order(lines))


def fetch_article_context(url: str, timeout: int = 25) -> ArticleContext:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with open_url(request, timeout) as response:
            final_url = response.geturl()
            content_type = response.headers.get("content-type", "")
            payload = response.read(1_500_000)
    except (urllib.error.URLError, socket.timeout) as exc:
        return ArticleContext(url, url, "", "", f"fetch failed: {exc}")

    if "html" not in content_type.lower() and not payload.lstrip().lower().startswith(b"<!doctype"):
        return ArticleContext(url, final_url, "", "", f"unsupported content type: {content_type}")

    text = decode_payload(payload, content_type)
    parser = ReadableHTMLParser()
    try:
        parser.feed(text)
    except Exception as exc:
        return ArticleContext(url, final_url, "", "", f"html parse failed: {exc}")

    article_text = parser.text
    if is_google_news_shell(final_url, article_text):
        return ArticleContext(
            url,
            final_url,
            parser.title,
            "",
            "Google News article shell did not expose the publisher article text.",
        )
    return ArticleContext(url, final_url, parser.title, article_text)


def open_url(request: urllib.request.Request, timeout: int):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        context = ssl._create_unverified_context()
        return urllib.request.urlopen(request, timeout=timeout, context=context)


def decode_payload(payload: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, flags=re.IGNORECASE)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "gb18030", "gbk"])
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def dedupe_keep_order(lines: list[str]) -> list[str]:
    seen = set()
    result = []
    for line in lines:
        key = re.sub(r"\W+", "", line.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def looks_like_navigation(line: str) -> bool:
    lower = line.lower()
    nav_terms = [
        "subscribe",
        "sign in",
        "cookie",
        "privacy policy",
        "terms of use",
        "advertisement",
        "skip to content",
        "all rights reserved",
    ]
    return any(term in lower for term in nav_terms)


def is_google_news_shell(final_url: str, text: str) -> bool:
    return "news.google.com" in final_url and len(text) < 1000

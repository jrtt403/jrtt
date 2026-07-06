from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote


@dataclass
class PublishedArticle:
    title: str
    slug: str
    source_path: Path
    html_path: Path
    url: str
    body_markdown: str
    body_html: str
    published_at: dt.datetime


def discover_articles(articles_dir: Path, selector: str) -> list[Path]:
    files = sorted(articles_dir.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    if selector == "latest":
        return files[:1]
    if selector == "all":
        return list(reversed(files))
    path = Path(selector)
    if not path.is_absolute():
        path = articles_dir.parent / selector
    if not path.exists():
        path = articles_dir / selector
    if not path.exists():
        raise FileNotFoundError(f"article not found: {selector}")
    return [path]


def build_public_site(
    articles_dir: Path,
    public_dir: Path,
    selector: str,
    base_url: str,
    channel_title: str = "普通人看懂中外热点背后的影响",
) -> list[PublishedArticle]:
    public_articles_dir = public_dir / "articles"
    public_articles_dir.mkdir(parents=True, exist_ok=True)
    selected_paths = discover_articles(articles_dir, selector)
    published = [
        render_article(path, public_articles_dir, base_url.rstrip("/"))
        for path in selected_paths
    ]
    write_index(public_dir / "index.html", published, channel_title)
    write_feed(public_dir / "feed.xml", published, channel_title, base_url.rstrip("/"))
    return published


def render_article(path: Path, output_dir: Path, base_url: str) -> PublishedArticle:
    raw = path.read_text(encoding="utf-8")
    title, article_markdown = extract_article(raw)
    slug = safe_slug(path.stem)
    body_html = markdown_to_html(article_markdown)
    html_path = output_dir / f"{slug}.html"
    published_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    url = f"{base_url}/articles/{quote(slug)}.html" if base_url else f"articles/{quote(slug)}.html"
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.75;
      max-width: 760px;
      margin: 40px auto;
      padding: 0 20px 56px;
      color: #1f2933;
    }}
    h1 {{ font-size: 30px; line-height: 1.25; margin-bottom: 24px; }}
    h2 {{ font-size: 22px; margin-top: 32px; }}
    p {{ margin: 14px 0; }}
    li {{ margin: 8px 0; }}
    .meta {{ color: #667085; font-size: 14px; margin-bottom: 28px; }}
  </style>
</head>
<body>
  <article>
    <h1>{html.escape(title)}</h1>
    <div class="meta">自动生成草稿，发布前请人工复核事实与表达。</div>
    {body_html}
  </article>
</body>
</html>
"""
    html_path.write_text(page, encoding="utf-8")
    return PublishedArticle(title, slug, path, html_path, url, article_markdown, body_html, published_at)


def extract_article(raw: str) -> tuple[str, str]:
    if "## 文章" in raw:
        body = raw.split("## 文章", 1)[1]
        if "## 发布前人工检查" in body:
            body = body.split("## 发布前人工检查", 1)[0]
    else:
        body = raw
    body = body.strip()
    lines = [line.rstrip() for line in body.splitlines()]
    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    if not title:
        title = "自动生成文章"
    return title, "\n".join(lines).strip()


def markdown_to_html(markdown: str) -> str:
    blocks: list[str] = []
    in_list = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            continue
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            if in_list:
                blocks.append("</ul>")
                in_list = False
            blocks.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif re.match(r"^[-*]\s+", line):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{inline_markdown(line[2:].strip())}</li>")
        elif re.match(r"^\d+\.\s+", line):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            item_text = re.sub(r"^\d+\.\s+", "", line)
            blocks.append(f"<li>{inline_markdown(item_text)}</li>")
        else:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            blocks.append(f"<p>{inline_markdown(line)}</p>")
    if in_list:
        blocks.append("</ul>")
    return "\n".join(blocks)


def inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def write_index(path: Path, articles: list[PublishedArticle], channel_title: str) -> None:
    items = "\n".join(
        f'<li><a href="{html.escape(article.url)}">{html.escape(article.title)}</a></li>'
        for article in articles
    )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(channel_title)}</title>
</head>
<body>
  <h1>{html.escape(channel_title)}</h1>
  <ul>
    {items}
  </ul>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")


def write_feed(
    path: Path,
    articles: list[PublishedArticle],
    channel_title: str,
    base_url: str,
) -> None:
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    items = "\n".join(feed_item(article) for article in articles)
    feed_url = f"{base_url}/feed.xml" if base_url else "feed.xml"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{xml_escape(channel_title)}</title>
    <link>{xml_escape(base_url or ".")}</link>
    <description>{xml_escape(channel_title)}</description>
    <lastBuildDate>{xml_escape(now)}</lastBuildDate>
    <atom:link xmlns:atom="http://www.w3.org/2005/Atom" href="{xml_escape(feed_url)}" rel="self" type="application/rss+xml" />
{items}
  </channel>
</rss>
"""
    path.write_text(xml, encoding="utf-8")


def feed_item(article: PublishedArticle) -> str:
    pub_date = format_datetime(article.published_at)
    return f"""    <item>
      <title>{xml_escape(article.title)}</title>
      <link>{xml_escape(article.url)}</link>
      <guid isPermaLink="true">{xml_escape(article.url)}</guid>
      <pubDate>{xml_escape(pub_date)}</pubDate>
      <description>{xml_escape(summary(article.body_markdown))}</description>
      <content:encoded><![CDATA[{article.body_html}]]></content:encoded>
    </item>"""


def summary(markdown: str) -> str:
    text = re.sub(r"#+\s*", "", markdown)
    text = re.sub(r"[*_`>\[\]()-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180]


def safe_slug(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:100] or "article"


def xml_escape(value: str) -> str:
    return html.escape(value, quote=True)

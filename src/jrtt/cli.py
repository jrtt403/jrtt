#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import ssl
import sqlite3
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from article_fetcher import ArticleContext, fetch_article_context
    from openai_client import OpenAIConfigError, OpenAIRequestError, generate_text
    from publisher import build_public_site
except ImportError:
    from .article_fetcher import ArticleContext, fetch_article_context
    from .openai_client import OpenAIConfigError, OpenAIRequestError, generate_text
    from .publisher import build_public_site


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DRAFTS_DIR = ROOT / "drafts"
ARTICLES_DIR = ROOT / "articles"
PUBLIC_DIR = ROOT / "public"
DEFAULT_BASE_URL = os.environ.get("JRTT_BASE_URL", "https://jrtt403.github.io/jrtt")
SOURCES_FILE = ROOT / "config" / "sources.json"
DB_FILE = DATA_DIR / "jrtt.db"


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            summary TEXT,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            source_weight INTEGER NOT NULL DEFAULT 3,
            published_at TEXT,
            fetched_at TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            score_detail TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    return conn


def load_sources() -> list[dict]:
    with SOURCES_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [item for item in data["sources"] if item.get("enabled", True)]


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "jrtt-ai-creator-mvp/0.1 (+local personal research)"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.read()
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=20, context=context) as response:
            return response.read()


def text_from_node(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return html.unescape(re.sub(r"\s+", " ", node.text)).strip()


def parse_datetime(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_rss(payload: bytes, source: dict) -> list[dict]:
    root = ET.fromstring(normalize_xml_payload(payload))
    items = []
    for item in root.findall(".//item"):
        title = text_from_node(item.find("title"))
        link = text_from_node(item.find("link"))
        summary = strip_tags(
            text_from_node(item.find("description"))
            or text_from_node(item.find("{http://purl.org/rss/1.0/modules/content/}encoded"))
        )
        published = parse_datetime(
            text_from_node(item.find("pubDate")) or text_from_node(item.find("published"))
        )
        if title and link:
            items.append(
                {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published_at": published,
                    "source": source["name"],
                    "category": source["category"],
                    "source_weight": int(source.get("weight", 3)),
                }
            )
    return items


def normalize_xml_payload(payload: bytes) -> bytes:
    head = payload[:200].decode("ascii", errors="ignore")
    match = re.search(r'encoding=["\']([^"\']+)["\']', head, flags=re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        text = payload.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        text = payload.decode("utf-8", errors="replace")
    text = re.sub(
        r'encoding=["\'][^"\']+["\']',
        'encoding="UTF-8"',
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return text.encode("utf-8")


def fingerprint(item: dict) -> str:
    normalized = re.sub(r"\W+", "", (item["title"] + item["link"]).lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def score_item(item: dict) -> tuple[int, dict]:
    now = dt.datetime.now(dt.timezone.utc)
    published_at = None
    if item.get("published_at"):
        try:
            published_at = dt.datetime.fromisoformat(item["published_at"])
        except ValueError:
            published_at = None

    age_hours = 72
    if published_at:
        age_hours = max(0, (now - published_at).total_seconds() / 3600)

    timeliness = 5 if age_hours <= 6 else 4 if age_hours <= 24 else 3 if age_hours <= 72 else 2
    title = item["title"]
    summary = item.get("summary") or ""
    text = f"{title} {summary}".lower()

    ordinary_keywords = [
        "price",
        "prices",
        "jobs",
        "employment",
        "policy",
        "trade",
        "market",
        "inflation",
        "税",
        "就业",
        "消费",
        "价格",
        "政策",
        "出行",
        "教育",
        "医保",
        "房",
    ]
    explanatory_keywords = [
        "why",
        "how",
        "impact",
        "because",
        "after",
        "amid",
        "影响",
        "原因",
        "背后",
        "变化",
        "趋势",
        "信号",
    ]
    risk_keywords = [
        "rumor",
        "unconfirmed",
        "graphic",
        "death toll",
        "quake",
        "earthquake",
        "rubble",
        "trapped",
        "disaster",
        "flood",
        "wildfire",
        "war",
        "strike",
        "strikes",
        "attack",
        "attacks",
        "military",
        "defence",
        "defense",
        "alliance",
        "missile",
        "ukraine",
        "russia",
        "crimea",
        "爆料",
        "网传",
        "内幕",
        "血腥",
        "战争",
        "冲突",
        "袭击",
        "打击",
        "导弹",
        "伤亡",
        "军事",
        "国防",
        "联盟",
        "地震",
        "废墟",
        "被困",
        "灾害",
        "洪水",
        "火灾",
        "荐股",
        "诊断",
    ]

    relevance = 4 if any(k in text for k in ordinary_keywords) else 3
    explain_space = 4 if any(k in text for k in explanatory_keywords) else 3
    risk = 3 if any(k in text for k in risk_keywords) else 4
    heat = min(5, max(1, item.get("source_weight", 3)))
    account_fit = 4 if item["category"] in {"international", "china"} else 3

    detail = {
        "heat": heat,
        "timeliness": timeliness,
        "ordinary_relevance": relevance,
        "explain_space": explain_space,
        "account_fit": account_fit,
        "compliance_safety": risk,
    }
    return sum(detail.values()), detail


def save_items(conn: sqlite3.Connection, items: list[dict]) -> int:
    inserted = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for item in items:
        score, detail = score_item(item)
        try:
            conn.execute(
                """
                INSERT INTO news_items (
                    fingerprint, title, link, summary, source, category,
                    source_weight, published_at, fetched_at, score, score_detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint(item),
                    item["title"],
                    item["link"],
                    item.get("summary"),
                    item["source"],
                    item["category"],
                    item.get("source_weight", 3),
                    item.get("published_at"),
                    now,
                    score,
                    json.dumps(detail, ensure_ascii=False),
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    return inserted


def fetch_all_sources() -> int:
    conn = connect_db()
    total = 0
    for source in load_sources():
        try:
            payload = fetch_url(source["url"])
            items = parse_rss(payload, source)
            inserted = save_items(conn, items)
            total += inserted
            print(f"{source['name']}: fetched={len(items)} inserted={inserted}")
        except Exception as exc:
            print(f"{source['name']}: failed: {exc}", file=sys.stderr)
    print(f"done: inserted={total} db={DB_FILE}")
    return total


def cmd_fetch(_: argparse.Namespace) -> None:
    fetch_all_sources()


def cmd_list(args: argparse.Namespace) -> None:
    conn = connect_db()
    rows = conn.execute(
        """
        SELECT id, title, source, category, published_at, score
        FROM news_items
        ORDER BY score DESC, COALESCE(published_at, fetched_at) DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for row in rows:
        published = row["published_at"] or "unknown time"
        print(f"#{row['id']} [{row['score']}] {row['title']}")
        print(f"    {row['source']} / {row['category']} / {published}")


def safe_filename(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value, flags=re.UNICODE)
    return value.strip("-")[:80] or "draft"


def cmd_draft(args: argparse.Namespace) -> None:
    conn = connect_db()
    row = conn.execute("SELECT * FROM news_items WHERE id = ?", (args.item_id,)).fetchone()
    if row is None:
        raise SystemExit(f"item not found: {args.item_id}")

    detail = json.loads(row["score_detail"])
    article_context = ArticleContext(row["link"], row["link"], "", "", "not fetched")
    ai_section = ""
    if args.ai:
        if args.enrich:
            print("fetching article context...", file=sys.stderr)
            article_context = fetch_article_context(row["link"])
            if article_context.ok:
                print(
                    f"article context fetched: {len(article_context.text)} chars",
                    file=sys.stderr,
                )
            else:
                print(f"article context unavailable: {article_context.error}", file=sys.stderr)
        print("calling OpenAI API for fact card and outline...", file=sys.stderr)
        ai_section = build_ai_section(row, detail, article_context, args.max_output_tokens)
        if has_placeholder_body(ai_section):
            print("AI omitted the body; requesting a body-only draft...", file=sys.stderr)
            ai_section += build_ai_body_section(row, article_context, args.max_output_tokens)

    today = dt.datetime.now().strftime("%Y-%m-%d")
    title = row["title"]
    filename = f"{today}-{row['id']}-{safe_filename(title)}.md"
    DRAFTS_DIR.mkdir(exist_ok=True)
    output = DRAFTS_DIR / filename
    content = f"""# {title}

> 定位：普通人看懂中外热点背后的影响

## 来源

- 来源：{row['source']}
- 链接：{row['link']}
- 最终链接：{article_context.final_url if article_context.final_url != row['link'] else '同上'}
- 原文抓取：{'成功' if article_context.ok else article_context.error}
- 发布时间：{row['published_at'] or '待核实'}
- 选题评分：{row['score']} / 30
- 评分明细：{json.dumps(detail, ensure_ascii=False)}

## 事实卡片

- 事件概述：{row['summary'] or '请补充权威来源摘要。'}
- 已确认事实：
  - 
- 待确认信息：
  - 
- 涉及相关方：
  - 

## 写作角度

- 这件事为什么重要：
- 对普通人的影响：
- 接下来需要关注的指标：

{ai_section}

## 标题候选

1. 这件事真正值得关注的，不是表面新闻
2. 普通人为什么要关注这次变化
3. 一个热点背后的几个信号

## 正文草稿

开头用 100-150 字说清楚事件和你的核心判断。

### 发生了什么

只写已确认事实，保留来源。

### 为什么重要

解释背景、利益关系、历史脉络。

### 对普通人有什么影响

从消费、就业、出行、汇率、产业、生活成本等角度展开。

### 接下来关注什么

给读者 2-3 个观察指标。

### 结尾

回到你的观点，形成一句有辨识度的总结。

## 发布前检查

- [ ] 是否核对了至少两个可靠来源
- [ ] 是否删除了未证实指控和煽动表达
- [ ] 是否避免了标题党
- [ ] 是否体现了自己的解释和判断
- [ ] 是否适合今日头条图文发布
"""
    output.write_text(content, encoding="utf-8")
    print(output)


def cmd_auto(args: argparse.Namespace) -> None:
    if args.fetch:
        fetch_all_sources()

    conn = connect_db()
    rows = find_auto_candidates(
        conn,
        args.candidate_limit,
        args.min_score,
        args.require_enriched,
        args.allow_risky,
    )
    if not rows:
        raise SystemExit("no suitable candidates found")

    generated = []
    skipped = []
    for row in rows:
        if len(generated) >= args.count:
            break
        print(f"candidate #{row['id']} [{row['score']}]: {row['title']}", file=sys.stderr)
        context = ArticleContext(row["link"], row["link"], "", "", "not fetched")
        if args.enrich:
            context = fetch_article_context(row["link"])
            if context.ok:
                print(f"  article context fetched: {len(context.text)} chars", file=sys.stderr)
            else:
                message = f"  skipped: article context unavailable: {context.error}"
                print(message, file=sys.stderr)
                skipped.append((row["id"], context.error))
                if args.require_enriched:
                    continue

        try:
            output = write_final_article(
                row,
                context,
                args.max_output_tokens,
                args.min_article_chars,
            )
        except (OpenAIConfigError, OpenAIRequestError) as exc:
            print(f"  failed: {exc}", file=sys.stderr)
            skipped.append((row["id"], str(exc)))
            continue
        generated.append(output)
        print(output)

    if not generated:
        skipped_text = "; ".join(f"#{item_id}: {reason}" for item_id, reason in skipped[:5])
        raise SystemExit(f"no article generated. skipped: {skipped_text}")
    if args.publish_feed:
        publish_articles("latest", args.base_url)
    if args.deploy:
        publish_articles("latest", args.base_url)
        deploy_to_github_pages(args.base_url, args.commit_message)


def find_auto_candidates(
    conn: sqlite3.Connection,
    limit: int,
    min_score: int,
    require_enriched: bool,
    allow_risky: bool,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM news_items
        WHERE score >= ?
        ORDER BY score DESC, COALESCE(published_at, fetched_at) DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    if not allow_risky:
        rows = [row for row in rows if not is_high_risk_topic(row)]
    if not require_enriched:
        return rows

    # Direct publisher links are more likely to provide usable article text than aggregator shells.
    preferred = [row for row in rows if "news.google.com" not in row["link"]]
    fallback = [row for row in rows if "news.google.com" in row["link"]]
    return preferred + fallback


def is_high_risk_topic(row: sqlite3.Row) -> bool:
    text = f"{row['title']} {row['summary'] or ''}".lower()
    high_risk_terms = [
        "war",
        "strike",
        "strikes",
        "attack",
        "attacks",
        "missile",
        "military",
        "defence",
        "defense",
        "alliance",
        "death toll",
        "killed",
        "quake",
        "earthquake",
        "rubble",
        "trapped",
        "disaster",
        "flood",
        "wildfire",
        "ukraine",
        "russia",
        "crimea",
        "israel",
        "gaza",
        "iran",
        "战争",
        "冲突",
        "袭击",
        "打击",
        "导弹",
        "军事",
        "国防",
        "联盟",
        "伤亡",
        "死亡",
        "地震",
        "废墟",
        "被困",
        "灾害",
        "洪水",
        "火灾",
    ]
    return any(term in text for term in high_risk_terms)


def write_final_article(
    row: sqlite3.Row,
    article_context: ArticleContext,
    max_output_tokens: int,
    min_article_chars: int,
) -> Path:
    detail = json.loads(row["score_detail"])
    text = build_final_article(row, detail, article_context, max_output_tokens, min_article_chars)
    if has_placeholder_body(text):
        text = build_ai_body_section(row, article_context, max_output_tokens)
    text = correct_numeric_errors(text, article_context)
    article_len = article_char_count(text)
    expand_attempts = 0
    while article_len < min_article_chars and expand_attempts < 3:
        expand_attempts += 1
        print(
            f"  article too short: {article_len} chars, expanding to at least {min_article_chars}",
            file=sys.stderr,
        )
        text = expand_final_article(
            row,
            article_context,
            text,
            min_article_chars,
            max(max_output_tokens, 3200),
        )
        text = correct_numeric_errors(text, article_context)
        article_len = article_char_count(text)
    if article_len < min_article_chars:
        raise OpenAIRequestError(
            f"generated article is still too short after expansion: {article_len} < {min_article_chars}"
        )

    today = dt.datetime.now().strftime("%Y-%m-%d")
    filename = f"{today}-{row['id']}-{safe_filename(row['title'])}.md"
    ARTICLES_DIR.mkdir(exist_ok=True)
    output = ARTICLES_DIR / filename
    content = f"""# 自动生成文章

## 选题来源

- 原标题：{row['title']}
- 来源：{row['source']}
- 链接：{row['link']}
- 最终链接：{article_context.final_url if article_context.final_url != row['link'] else '同上'}
- 原文抓取：{'成功' if article_context.ok else article_context.error}
- 发布时间：{row['published_at'] or '待核实'}
- 选题评分：{row['score']} / 30
- 评分明细：{json.dumps(detail, ensure_ascii=False)}
- 文章长度：{article_len} 字符
- 最低要求：{min_article_chars} 字符

## 文章

{text}

## 发布前人工检查

- [ ] 至少核对 2 个可靠来源
- [ ] 删除未证实指控、煽动表达和夸张标题
- [ ] 确认事实、金额、机构名、人名无误
- [ ] 确认文章不是简单搬运或洗稿
- [ ] 确认适合今日头条发布
"""
    output.write_text(content, encoding="utf-8")
    return output


def cmd_publish(args: argparse.Namespace) -> None:
    publish_articles(args.article, args.base_url)


def publish_articles(selector: str, base_url: str) -> None:
    published = build_public_site(
        articles_dir=ARTICLES_DIR,
        public_dir=PUBLIC_DIR,
        selector=selector,
        base_url=base_url,
    )
    print(PUBLIC_DIR / "feed.xml")
    for article in published:
        print(article.html_path)


def cmd_deploy(args: argparse.Namespace) -> None:
    publish_articles(args.article, args.base_url)
    deploy_to_github_pages(args.base_url, args.message)


def deploy_to_github_pages(base_url: str, message: str) -> None:
    run_git(["add", "articles", "public", "README.md", "WORK_CONTEXT.md", "docs", "src", "scripts"])
    staged = run_git(["diff", "--cached", "--name-only"], capture=True)
    if not staged.strip():
        print("nothing to deploy")
        print(f"{base_url.rstrip('/')}/feed.xml")
        return
    run_git(["commit", "-m", message])
    run_git(["push"])
    print(f"{base_url.rstrip('/')}/feed.xml")


def run_git(args: list[str], capture: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    return result.stdout or ""


def build_final_article(
    row: sqlite3.Row,
    detail: dict,
    article_context: ArticleContext,
    max_output_tokens: int,
    min_article_chars: int,
) -> str:
    instructions = """你是一个谨慎、平实的中文原创文章编辑，服务于今日头条图文创作者。
账号定位：普通人看懂中外热点背后的影响。
请直接输出可发布前审核的完整文章，不要输出创作说明、过程、大纲或占位语。
必须区分事实与判断，不编造未提供的信息；不确定内容要写“仍需观察”或“需要进一步确认”。
涉及金额、数量、日期、机构名、人名时，必须严格照抄材料中的表达；不要自行换算、扩大单位或改写数字。
避免标题党、煽动性表达、阴谋论、投资建议和医疗法律等专业建议。"""
    article_status = "成功" if article_context.ok else article_context.error
    user_input = f"""请根据以下材料，直接写一篇不少于 {min_article_chars} 个中文字符的原创文章，建议 1200-1800 字。

原始标题：{row['title']}
来源：{row['source']}
链接：{row['link']}
最终链接：{article_context.final_url}
发布时间：{row['published_at'] or '未知'}
摘要：{row['summary'] or '无摘要'}
本地评分：{row['score']} / 30
评分明细：{json.dumps(detail, ensure_ascii=False)}
原文抓取状态：{article_status}

可核验页面标题：
{article_context.title or '无'}

可核验页面文本：
{article_context.excerpt(7000) or '无。请明确提醒读者该信息仍需人工补充来源，不要补写具体细节。'}

输出格式：
# 标题
标题要有判断，但不夸张。

正文结构：
1. 开头：100-150 字说清事件和核心判断
2. 发生了什么：只写材料支持的事实
3. 为什么重要：解释背景和影响
4. 对普通人有什么影响：从消费、就业、生活成本、商业规则或法律意识等角度选择合适方向
5. 接下来关注什么：给 2-3 个观察点
6. 结尾：一句有辨识度的总结

必须直接写完整文章，不要写“此处省略”“请根据大纲撰写”。文章长度必须不小于 {min_article_chars} 个中文字符。"""
    return generate_text(
        instructions=instructions,
        user_input=user_input,
        max_output_tokens=max_output_tokens,
    )


def article_char_count(text: str) -> int:
    without_markdown = re.sub(r"[#>*_`\\-\\[\\]()]", "", text)
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", without_markdown))


def correct_numeric_errors(text: str, article_context: ArticleContext) -> str:
    context = article_context.text.lower()
    if "10.3m yuan" in context:
        text = text.replace("10.3亿元人民币", "1030万元人民币")
        text = text.replace("10.3亿元", "1030万元人民币")
    return text


def expand_final_article(
    row: sqlite3.Row,
    article_context: ArticleContext,
    current_article: str,
    min_article_chars: int,
    max_output_tokens: int,
) -> str:
    instructions = """你是一个中文原创文章编辑。
请把给定文章扩写成完整、自然、可发布前审核的文章。
必须保留事实准确性，不能新增材料中没有的具体数字、日期、人名、机构名。
可以扩展背景解释、普通人影响、接下来观察点和结尾判断。
直接输出完整扩写后的文章，不要解释修改过程。"""
    target_chars = min_article_chars + 200
    user_input = f"""请把下面文章扩写到不少于 {target_chars} 个中文字符。

原始标题：{row['title']}
来源：{row['source']}
链接：{row['link']}
原文抓取状态：{'成功' if article_context.ok else article_context.error}
可核验页面文本：
{article_context.excerpt(7000) or '无。请不要补写具体细节。'}

当前文章：
{current_article}

要求：
1. 最终文章不小于 {target_chars} 个中文字符
2. 保留 Markdown 标题和分段
3. 不要写“此处省略”
4. 不要自行换算金额或改写数字单位；如果材料写的是 10.3m yuan，只能写 1030万元人民币或保留 10.3m yuan，不能写成 10.3亿元
5. 内容要更具体，但只能基于材料扩展解释和影响"""
    return generate_text(
        instructions=instructions,
        user_input=user_input,
        max_output_tokens=max_output_tokens,
    )


def build_ai_section(
    row: sqlite3.Row,
    detail: dict,
    article_context: ArticleContext,
    max_output_tokens: int,
) -> str:
    instructions = """你是一个谨慎的中文新闻解释型内容编辑，服务于今日头条图文创作者。
账号定位：普通人看懂中外热点背后的影响。
你必须区分事实、推测和写作建议；不要编造来源；不要使用标题党、煽动、阴谋论表达。
输出中文 Markdown，只生成可编辑草稿材料。若“可核验页面文本”为空或不足，必须明确提醒需要人工补充来源，不要把缺失信息写成事实。
必须写出完整的可编辑正文初稿，禁止写“此处省略”“根据大纲撰写”等占位语。若输出空间不足，优先保留完整正文，压缩标题候选和大纲。"""
    article_excerpt = article_context.excerpt()
    article_status = "成功" if article_context.ok else article_context.error
    user_input = f"""请基于以下热点条目，生成今日头条图文创作材料。

标题：{row['title']}
来源：{row['source']}
分类：{row['category']}
链接：{row['link']}
最终链接：{article_context.final_url}
发布时间：{row['published_at'] or '未知'}
摘要：{row['summary'] or '无摘要'}
本地评分：{row['score']} / 30
评分明细：{json.dumps(detail, ensure_ascii=False)}
原文抓取状态：{article_status}

可核验页面标题：
{article_context.title or '无'}

可核验页面文本：
{article_excerpt or '无。请只基于标题、摘要和明确可见的信息生成材料，并提示人工补充权威来源。'}

请输出以下部分：
1. 事实卡片：事件概述、已确认事实、待确认信息、相关方
2. 选题判断：为什么值得写、普通人相关性、主要风险
3. 写作角度：3 个角度，每个角度说明适合怎么写
4. 标题候选：8 个，避免夸张标题党
5. 文章大纲：开头、发生了什么、为什么重要、普通人影响、接下来关注什么、结尾
6. 可编辑正文初稿：800-1200 字，语气平实、有判断，不编造事实。必须直接写完整正文，不能省略。
"""
    try:
        text = generate_text(
            instructions=instructions,
            user_input=user_input,
            max_output_tokens=max_output_tokens,
        )
    except (OpenAIConfigError, OpenAIRequestError) as exc:
        raise SystemExit(str(exc)) from exc
    return f"""## AI 辅助材料

{text}
"""


def has_placeholder_body(text: str) -> bool:
    placeholders = ["此处省略", "根据大纲撰写", "正文内容将", "请根据以上大纲"]
    return any(item in text for item in placeholders)


def build_ai_body_section(
    row: sqlite3.Row,
    article_context: ArticleContext,
    max_output_tokens: int,
) -> str:
    instructions = """你是一个中文新闻解释型内容编辑。
只输出一篇完整的今日头条图文正文，不要输出大纲，不要输出说明，不要使用占位语。
文章必须基于给定事实，不能编造未提供的信息；不确定的地方写“仍需观察”或“需要进一步确认”。
语气平实，有解释和判断，避免标题党。"""
    user_input = f"""请写一篇 900-1200 字中文正文。

标题：{row['title']}
来源：{row['source']}
链接：{row['link']}
摘要：{row['summary'] or '无摘要'}
原文抓取状态：{'成功' if article_context.ok else article_context.error}
可核验页面文本：
{article_context.excerpt() or '无。请提醒需要人工补充来源。'}

正文结构：
1. 开头：一句话说清事件和核心判断
2. 发生了什么
3. 为什么重要
4. 对普通人有什么影响
5. 接下来关注什么
6. 结尾总结

必须直接输出完整正文，不要写“此处省略”。"""
    try:
        text = generate_text(
            instructions=instructions,
            user_input=user_input,
            max_output_tokens=max_output_tokens,
        )
    except (OpenAIConfigError, OpenAIRequestError) as exc:
        raise SystemExit(str(exc)) from exc
    return f"""

## AI 完整正文补写

{text}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="今日头条 AI 辅助创作 MVP")
    subparsers = parser.add_subparsers(required=True)

    fetch_parser = subparsers.add_parser("fetch", help="抓取配置的 RSS 热点")
    fetch_parser.set_defaults(func=cmd_fetch)

    list_parser = subparsers.add_parser("list", help="查看候选选题")
    list_parser.add_argument("--limit", type=int, default=10)
    list_parser.set_defaults(func=cmd_list)

    draft_parser = subparsers.add_parser("draft", help="为指定选题生成草稿骨架")
    draft_parser.add_argument("--item-id", type=int, required=True)
    draft_parser.add_argument("--ai", action="store_true", help="调用 OpenAI API 生成事实卡片、大纲和初稿")
    draft_parser.add_argument(
        "--no-enrich",
        dest="enrich",
        action="store_false",
        help="不抓取原文页面，只使用 RSS 标题和摘要",
    )
    draft_parser.add_argument("--max-output-tokens", type=int, default=2500)
    draft_parser.set_defaults(enrich=True)
    draft_parser.set_defaults(func=cmd_draft)

    auto_parser = subparsers.add_parser("auto", help="自动抓取、选题并生成完整文章")
    auto_parser.add_argument("--count", type=int, default=1, help="生成文章数量")
    auto_parser.add_argument("--candidate-limit", type=int, default=20, help="最多检查多少个候选选题")
    auto_parser.add_argument("--min-score", type=int, default=23, help="最低选题评分")
    auto_parser.add_argument("--no-fetch", dest="fetch", action="store_false", help="不先抓取最新热点")
    auto_parser.add_argument(
        "--no-enrich",
        dest="enrich",
        action="store_false",
        help="不抓取原文页面，只使用 RSS 标题和摘要",
    )
    auto_parser.add_argument(
        "--allow-unenriched",
        dest="require_enriched",
        action="store_false",
        help="原文抓取失败时也允许生成文章",
    )
    auto_parser.add_argument("--allow-risky", action="store_true", help="允许战争、冲突、伤亡等高风险选题")
    auto_parser.add_argument("--max-output-tokens", type=int, default=2600)
    auto_parser.add_argument("--min-article-chars", type=int, default=1000, help="生成文章最低字符数")
    auto_parser.add_argument("--publish-feed", action="store_true", help="生成文章后同步更新 public/feed.xml")
    auto_parser.add_argument("--deploy", action="store_true", help="生成文章后提交并推送到 GitHub Pages")
    auto_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="公网发布根地址")
    auto_parser.add_argument("--commit-message", default="Add generated article", help="自动部署的 git commit 信息")
    auto_parser.set_defaults(fetch=True, enrich=True, require_enriched=True, allow_risky=False)
    auto_parser.set_defaults(func=cmd_auto)

    publish_parser = subparsers.add_parser("publish", help="把文章打包为头条内容源 RSS/HTML")
    publish_parser.add_argument(
        "--article",
        default="latest",
        help="latest、all，或指定 articles/ 下的 Markdown 文件",
    )
    publish_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="公网发布根地址")
    publish_parser.set_defaults(func=cmd_publish)

    deploy_parser = subparsers.add_parser("deploy", help="更新内容源并推送到 GitHub Pages")
    deploy_parser.add_argument(
        "--article",
        default="latest",
        help="latest、all，或指定 articles/ 下的 Markdown 文件",
    )
    deploy_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="公网发布根地址")
    deploy_parser.add_argument("--message", default="Publish generated article", help="git commit 信息")
    deploy_parser.set_defaults(func=cmd_deploy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

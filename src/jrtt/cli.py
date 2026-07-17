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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

try:
    from article_fetcher import ArticleContext, fetch_article_context
    from metrics import (
        import_metrics_csv,
        render_title_metrics_report,
        title_feedback_summary,
        write_metrics_template,
    )
    from openai_client import OpenAIConfigError, OpenAIRequestError, generate_text
    from publisher import build_public_site
except ImportError:
    from .article_fetcher import ArticleContext, fetch_article_context
    from .metrics import (
        import_metrics_csv,
        render_title_metrics_report,
        title_feedback_summary,
        write_metrics_template,
    )
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


def article_date_string() -> str:
    override = os.environ.get("JRTT_ARTICLE_DATE")
    if override:
        try:
            dt.date.fromisoformat(override)
        except ValueError as exc:
            raise SystemExit("JRTT_ARTICLE_DATE must use YYYY-MM-DD") from exc
        return override
    return dt.datetime.now().strftime("%Y-%m-%d")


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


def stable_toutiao_link(item: dict) -> str:
    raw_url = str(item.get("Url") or item.get("url") or "").strip()
    cluster_id = str(item.get("ClusterIdStr") or item.get("ClusterId") or "").strip()
    if not raw_url and cluster_id:
        return f"https://www.toutiao.com/trending/{cluster_id}/"

    if raw_url.startswith("//"):
        raw_url = f"https:{raw_url}"
    elif raw_url.startswith("/"):
        raw_url = f"https://www.toutiao.com{raw_url}"

    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme and parsed.netloc:
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return raw_url


def compact_toutiao_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "、".join(part for part in (compact_toutiao_value(item) for item in value) if part)
    if isinstance(value, dict):
        return "、".join(
            part for part in (compact_toutiao_value(item) for item in value.values()) if part
        )
    return str(value).strip()


def parse_toutiao_hot(payload: bytes, source: dict) -> list[dict]:
    data = json.loads(payload.decode("utf-8", errors="replace"))
    raw_items = data.get("data", [])
    if isinstance(raw_items, dict):
        for key in ("data", "list", "items"):
            if isinstance(raw_items.get(key), list):
                raw_items = raw_items[key]
                break
    if not isinstance(raw_items, list):
        raise ValueError("unexpected toutiao hot payload")

    max_items = int(source.get("max_items", 50))
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    items = []
    for rank, item in enumerate(raw_items[:max_items], 1):
        if not isinstance(item, dict):
            continue
        title = str(
            item.get("Title")
            or item.get("title")
            or item.get("QueryWord")
            or item.get("word")
            or ""
        ).strip()
        link = stable_toutiao_link(item)
        if not title or not link:
            continue

        hot_value = compact_toutiao_value(item.get("HotValue"))
        raw_label = compact_toutiao_value(item.get("Label"))
        label = {
            "new": "新上榜",
            "recentProgress": "最新进展",
            "onSite": "现场",
        }.get(raw_label, raw_label)
        query_word = compact_toutiao_value(item.get("QueryWord"))
        interest = compact_toutiao_value(item.get("InterestCategory"))
        summary_parts = [f"今日头条热榜第{rank}名"]
        if hot_value:
            summary_parts.append(f"热度{hot_value}")
        if label:
            summary_parts.append(f"标签{label}")
        if interest:
            summary_parts.append(f"分类{interest}")
        if query_word and query_word != title:
            summary_parts.append(f"搜索词：{query_word}")

        items.append(
            {
                "title": title,
                "link": link,
                "summary": "，".join(summary_parts),
                "published_at": fetched_at,
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
        "cost",
        "costs",
        "shopping",
        "travel",
        "tourism",
        "summer",
        "ticket",
        "tickets",
        "consumer",
        "consumers",
        "family",
        "families",
        "school",
        "students",
        "ai",
        "robot",
        "robots",
        "ev",
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
        "省钱",
        "涨价",
        "降价",
        "补贴",
        "购物",
        "旅游",
        "暑期",
        "门票",
        "家庭",
        "孩子",
        "学生",
        "工资",
        "收入",
        "机器人",
        "电动车",
        "手机",
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
    weak_click_keywords = [
        "president",
        "minister",
        "foreign minister",
        "secretary-general",
        "spokesperson",
        "summit",
        "meeting",
        "congratulates",
        "delegation",
        "diplomatic",
        "ties",
        "cooperation",
        "presidential",
        "总统",
        "外长",
        "秘书长",
        "发言人",
        "峰会",
        "会晤",
        "代表团",
        "外交",
        "双边",
        "合作",
        "建交",
        "元首",
        "讲话",
        "致贺",
        "高官",
        "点赞",
        "模式",
    ]
    risk_keywords = [
        "rumor",
        "unconfirmed",
        "graphic",
        "death toll",
        "deadly",
        "fatal",
        "dead",
        "dies",
        "died",
        "illness",
        "sudden illness",
        "passes away",
        "fire",
        "quake",
        "earthquake",
        "rubble",
        "trapped",
        "disaster",
        "flood",
        "wildfire",
        "storm",
        "storms",
        "severe weather",
        "rescue underway",
        "tornado",
        "hurricane",
        "typhoon",
        "maysak",
        "heat wave",
        "heatwave",
        "extreme heat",
        "vanished",
        "missing plane",
        "cargo plane",
        "plane crash",
        "crash",
        "war",
        "strike",
        "strikes",
        "attack",
        "attacks",
        "shooting",
        "shot",
        "sniper",
        "suspect",
        "suspects",
        "prosecutor",
        "prosecutors",
        "arrested",
        "shooter",
        "gunman",
        "gunfire",
        "murder",
        "accused",
        "indicted",
        "criminal",
        "crime",
        "fentanyl",
        "drug",
        "drugs",
        "cartel",
        "trafficking",
        "assassination",
        "assassinated",
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
        "枪击",
        "中枪",
        "狙击",
        "嫌疑人",
        "检方",
        "检察官",
        "逮捕",
        "枪手",
        "遇刺",
        "谋杀",
        "凶手",
        "刺杀",
        "伤亡",
        "死亡",
        "致死",
        "遇难",
        "去世",
        "病逝",
        "病亡",
        "被控",
        "指控",
        "起诉",
        "犯罪",
        "毒品",
        "贩毒",
        "芬太尼",
        "走私",
        "军事",
        "国防",
        "联盟",
        "地震",
        "废墟",
        "被困",
        "灾害",
        "洪水",
        "火灾",
        "风暴",
        "强风暴",
        "暴雨",
        "救援",
        "龙卷风",
        "飓风",
        "台风",
        "高温",
        "热浪",
        "极端天气",
        "失踪",
        "坠机",
        "空难",
        "货机",
        "荐股",
        "诊断",
    ]

    relevance = 4 if any(k in text for k in ordinary_keywords) else 3
    explain_space = 4 if any(k in text for k in explanatory_keywords) else 3
    risk = 3 if any(k in text for k in risk_keywords) else 4
    heat = min(5, max(1, item.get("source_weight", 3)))
    account_fit = 4 if item["category"] in {"international", "china"} else 3
    traffic_bonus = 2 if any(k in text for k in ordinary_keywords) else 0
    weak_click_penalty = 3 if any(k in text for k in weak_click_keywords) else 0

    detail = {
        "heat": heat,
        "timeliness": timeliness,
        "ordinary_relevance": relevance,
        "explain_space": explain_space,
        "account_fit": account_fit,
        "compliance_safety": risk,
        "traffic_bonus": traffic_bonus,
        "weak_click_penalty": -weak_click_penalty,
    }
    score = min(30, max(1, sum(detail.values())))
    return score, detail


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
            source_type = source.get("type", "rss")
            if source_type == "toutiao_hot":
                items = parse_toutiao_hot(payload, source)
            elif source_type == "rss":
                items = parse_rss(payload, source)
            else:
                raise ValueError(f"unsupported source type: {source_type}")
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

    today = article_date_string()
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
        args.category,
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
                args.title_optimize,
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
    category: str | None = None,
) -> list[sqlite3.Row]:
    category_filter = ""
    params: list[object] = [min_score]
    if category:
        category_filter = "AND category = ?"
        params.append(category)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM news_items
        WHERE score >= ?
        {category_filter}
        ORDER BY score DESC, COALESCE(published_at, fetched_at) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    rows = [row for row in rows if not article_exists_for_item(row["id"])]
    if not allow_risky:
        rows = [row for row in rows if not is_high_risk_topic(row)]
    if not require_enriched:
        return rows

    # Direct publisher links are more likely to provide usable article text than aggregator shells.
    preferred = [row for row in rows if "news.google.com" not in row["link"]]
    fallback = [row for row in rows if "news.google.com" in row["link"]]
    return preferred + fallback


def article_exists_for_item(item_id: int) -> bool:
    return any(ARTICLES_DIR.glob(f"*-{item_id}-*.md"))


def is_high_risk_topic(row: sqlite3.Row) -> bool:
    text = f"{row['title']} {row['summary'] or ''}".lower()
    high_risk_terms = [
        "war",
        "strike",
        "strikes",
        "attack",
        "attacks",
        "shooting",
        "shot",
        "sniper",
        "suspect",
        "suspects",
        "prosecutor",
        "prosecutors",
        "arrested",
        "shooter",
        "gunman",
        "gunfire",
        "murder",
        "accused",
        "indicted",
        "criminal",
        "crime",
        "fentanyl",
        "drug",
        "drugs",
        "cartel",
        "trafficking",
        "assassination",
        "assassinated",
        "missile",
        "military",
        "defence",
        "defense",
        "alliance",
        "death toll",
        "deadly",
        "fatal",
        "dead",
        "dies",
        "died",
        "illness",
        "sudden illness",
        "passes away",
        "fire",
        "killed",
        "quake",
        "earthquake",
        "rubble",
        "trapped",
        "disaster",
        "flood",
        "wildfire",
        "storm",
        "storms",
        "severe weather",
        "rescue underway",
        "tornado",
        "hurricane",
        "typhoon",
        "maysak",
        "heat wave",
        "heatwave",
        "extreme heat",
        "vanished",
        "missing plane",
        "cargo plane",
        "plane crash",
        "crash",
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
        "枪击",
        "中枪",
        "狙击",
        "嫌疑人",
        "检方",
        "检察官",
        "逮捕",
        "枪手",
        "遇刺",
        "谋杀",
        "凶手",
        "刺杀",
        "军事",
        "国防",
        "联盟",
        "伤亡",
        "死亡",
        "致死",
        "遇难",
        "去世",
        "病逝",
        "病亡",
        "被控",
        "指控",
        "起诉",
        "犯罪",
        "毒品",
        "贩毒",
        "芬太尼",
        "走私",
        "地震",
        "废墟",
        "被困",
        "灾害",
        "洪水",
        "火灾",
        "风暴",
        "强风暴",
        "暴雨",
        "救援",
        "龙卷风",
        "飓风",
        "台风",
        "高温",
        "热浪",
        "极端天气",
        "失踪",
        "坠机",
        "空难",
        "货机",
    ]
    return any(term in text for term in high_risk_terms)


@dataclass
class PublishedArticleSeed:
    path: Path
    title: str
    original_title: str
    source: str
    link: str
    published_at: str | None
    article_text: str
    article_date: dt.date


@dataclass
class FollowUpCandidate:
    seed: PublishedArticleSeed
    row: sqlite3.Row
    similarity: float
    reason: str


def cmd_followup(args: argparse.Namespace) -> None:
    if args.fetch:
        fetch_all_sources()

    conn = connect_db()
    seeds = load_published_article_seeds(
        ARTICLES_DIR,
        args.lookback_days,
        args.include_followups,
    )
    if not seeds:
        print("no recent published articles found for follow-up check")
        return

    candidates = find_followup_candidates(
        conn,
        seeds,
        args.candidate_limit,
        args.min_score,
        args.min_similarity,
        args.allow_risky,
    )
    if not candidates:
        print("no follow-up candidates found")
        return

    if args.dry_run:
        for candidate in candidates[: args.count]:
            print(format_followup_candidate(candidate))
        return

    generated: list[Path] = []
    skipped: list[tuple[int, str]] = []
    for candidate in candidates:
        if len(generated) >= args.count:
            break
        row = candidate.row
        print(
            f"follow-up candidate #{row['id']} similarity={candidate.similarity:.2f}: {row['title']}",
            file=sys.stderr,
        )
        context = ArticleContext(row["link"], row["link"], "", "", "not fetched")
        if args.enrich:
            context = fetch_article_context(row["link"])
            if context.ok:
                print(f"  article context fetched: {len(context.text)} chars", file=sys.stderr)
            elif args.require_enriched:
                print(f"  skipped: article context unavailable: {context.error}", file=sys.stderr)
                skipped.append((row["id"], context.error))
                continue

        try:
            output = write_followup_article(
                candidate,
                context,
                args.max_output_tokens,
                args.min_article_chars,
                args.title_optimize,
            )
        except (OpenAIConfigError, OpenAIRequestError) as exc:
            print(f"  failed: {exc}", file=sys.stderr)
            skipped.append((row["id"], str(exc)))
            continue
        generated.append(output)
        print(output)

    if not generated:
        skipped_text = "; ".join(f"#{item_id}: {reason}" for item_id, reason in skipped[:5])
        print(f"no follow-up generated. skipped: {skipped_text}")
        return
    if args.publish_feed:
        publish_articles("latest", args.base_url)
    if args.deploy:
        publish_articles("latest", args.base_url)
        deploy_to_github_pages(args.base_url, args.commit_message)


def load_published_article_seeds(
    articles_dir: Path,
    lookback_days: int,
    include_followups: bool,
) -> list[PublishedArticleSeed]:
    cutoff = dt.datetime.now().date() - dt.timedelta(days=lookback_days)
    seeds: list[PublishedArticleSeed] = []
    for path in sorted(articles_dir.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
        raw = path.read_text(encoding="utf-8")
        if not include_followups and "## 追更来源" in raw:
            continue
        article_date = parse_article_file_date(path) or dt.datetime.fromtimestamp(path.stat().st_mtime).date()
        if article_date < cutoff:
            continue
        article_text = section_between(raw, "## 文章", "## 发布前人工检查") or raw
        title = extract_article_title(article_text) or extract_metadata_value(raw, "选中标题") or path.stem
        original_title = extract_metadata_value(raw, "原标题") or title
        link = extract_metadata_value(raw, "链接")
        seeds.append(
            PublishedArticleSeed(
                path=path,
                title=title,
                original_title=original_title,
                source=extract_metadata_value(raw, "来源"),
                link=link,
                published_at=extract_metadata_value(raw, "发布时间") or None,
                article_text=article_text,
                article_date=article_date,
            )
        )
    return seeds


def parse_article_file_date(path: Path) -> dt.date | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})-", path.name)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def extract_metadata_value(raw: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}：(.+)$", raw, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def section_between(raw: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in raw:
        return ""
    section = raw.split(start_marker, 1)[1]
    if end_marker in section:
        section = section.split(end_marker, 1)[0]
    return section.strip()


def normalize_match_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").lower(), flags=re.UNICODE)


def find_followup_candidates(
    conn: sqlite3.Connection,
    seeds: list[PublishedArticleSeed],
    limit: int,
    min_score: int,
    min_similarity: float,
    allow_risky: bool,
) -> list[FollowUpCandidate]:
    rows = conn.execute(
        """
        SELECT *
        FROM news_items
        WHERE score >= ?
        ORDER BY COALESCE(published_at, fetched_at) DESC, score DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    candidates: list[FollowUpCandidate] = []
    seen_item_ids: set[int] = set()
    for row in rows:
        if article_exists_for_item(row["id"]):
            continue
        if not allow_risky and is_high_risk_topic(row):
            continue
        best: FollowUpCandidate | None = None
        for seed in seeds:
            if is_same_source_item(seed, row):
                continue
            if not is_newer_than_seed(seed, row):
                continue
            similarity, reason = followup_similarity(seed, row)
            if similarity < min_similarity:
                continue
            candidate = FollowUpCandidate(seed, row, similarity, reason)
            if best is None or candidate.similarity > best.similarity:
                best = candidate
        if best and row["id"] not in seen_item_ids:
            seen_item_ids.add(row["id"])
            candidates.append(best)
    candidates.sort(
        key=lambda item: (
            item.similarity,
            item.row["score"],
            item.row["published_at"] or "",
        ),
        reverse=True,
    )
    return candidates


def is_same_source_item(seed: PublishedArticleSeed, row: sqlite3.Row) -> bool:
    seed_link = normalize_match_key(seed.link)
    row_link = normalize_match_key(row["link"])
    if seed_link and seed_link == row_link:
        return True
    seed_title = normalize_match_key(seed.original_title)
    row_title = normalize_match_key(row["title"])
    return bool(seed_title and seed_title == row_title)


def is_newer_than_seed(seed: PublishedArticleSeed, row: sqlite3.Row) -> bool:
    row_time = parse_iso_datetime(row["published_at"]) or parse_iso_datetime(row["fetched_at"])
    seed_time = parse_iso_datetime(seed.published_at)
    if seed_time is None:
        seed_time = dt.datetime.combine(seed.article_date, dt.time.min, tzinfo=dt.timezone.utc)
    if row_time is None:
        return True
    return row_time > seed_time + dt.timedelta(hours=3)


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value or value in {"待核实", "未知"}:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def followup_similarity(seed: PublishedArticleSeed, row: sqlite3.Row) -> tuple[float, str]:
    seed_text = f"{seed.original_title} {seed.title}"
    row_text = f"{row['title']} {row['summary'] or ''}"
    seed_key = normalize_match_key(seed_text)
    row_key = normalize_match_key(row_text)
    sequence_ratio = SequenceMatcher(None, seed_key, row_key).ratio() if seed_key and row_key else 0.0
    seed_tokens = topic_tokens(seed_text)
    row_tokens = topic_tokens(row_text)
    overlap = seed_tokens & row_tokens
    if not overlap and sequence_ratio < 0.45:
        return 0.0, f"sequence={sequence_ratio:.2f}, no topic overlap"
    if len(overlap) < 2 and sequence_ratio < 0.42:
        return 0.0, f"sequence={sequence_ratio:.2f}, weak topic overlap={', '.join(sorted(overlap)) or 'none'}"
    union = seed_tokens | row_tokens
    token_ratio = len(overlap) / len(union) if union else 0.0
    entity_ratio = len({token for token in overlap if len(token) >= 4}) / max(
        1,
        len({token for token in seed_tokens if len(token) >= 4}),
    )
    similarity = max(sequence_ratio, token_ratio * 0.55 + entity_ratio * 0.45)
    if seed.source and seed.source == row["source"]:
        similarity += 0.04
    similarity = min(1.0, similarity)
    reason = (
        f"sequence={sequence_ratio:.2f}, token={token_ratio:.2f}, "
        f"entity={entity_ratio:.2f}, overlap={', '.join(sorted(overlap)[:8]) or 'none'}"
    )
    return similarity, reason


def topic_tokens(value: str) -> set[str]:
    value = value.lower()
    english = re.findall(r"[a-z][a-z0-9-]{2,}", value)
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    chinese_grams: list[str] = []
    for chunk in chinese_chunks:
        if len(chunk) <= 4:
            chinese_grams.append(chunk)
        else:
            chinese_grams.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
            chinese_grams.extend(chunk[index : index + 3] for index in range(len(chunk) - 2))
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "after",
        "over",
        "this",
        "that",
        "what",
        "why",
        "how",
        "china",
        "chinese",
        "中国",
        "为何",
        "什么",
        "影响",
        "背后",
        "一个",
        "这件",
        "普通人",
    }
    return {token for token in [*english, *chinese_grams] if token not in stopwords}


def format_followup_candidate(candidate: FollowUpCandidate) -> str:
    row = candidate.row
    return (
        f"#{row['id']} similarity={candidate.similarity:.2f} score={row['score']} "
        f"seed={candidate.seed.title} -> {row['title']}\n"
        f"    reason: {candidate.reason}\n"
        f"    link: {row['link']}"
    )


@dataclass
class TitleCandidate:
    title: str
    score: int = 0
    reason: str = ""


@dataclass
class TitleOptimizationResult:
    selected: str
    candidates: list[TitleCandidate]

    @classmethod
    def from_article_text(cls, text: str) -> "TitleOptimizationResult":
        title = normalize_title(extract_article_title(text) or "自动生成文章")
        return cls(title, [TitleCandidate(title, 0, "正文原始标题")])

    def to_markdown(self) -> str:
        lines = ["## 标题优化", "", f"- 选中标题：{self.selected}", "- 候选标题："]
        for index, candidate in enumerate(self.candidates, start=1):
            reason = f"；{candidate.reason}" if candidate.reason else ""
            score = f"{candidate.score}分" if candidate.score else "未评分"
            lines.append(f"  {index}. {candidate.title}（{score}{reason}）")
        return "\n".join(lines)


def extract_article_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def replace_article_title(text: str, title: str) -> str:
    title = normalize_title(title)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("# "):
            lines[index] = f"# {title}"
            return "\n".join(lines).strip()
    return f"# {title}\n\n{text.strip()}"


def normalize_title(title: str, max_chars: int = 30) -> str:
    title = re.sub(r"^[#\s：:、，。.!！?？\"'“”‘’]+", "", title.strip())
    title = re.sub(r"\s+", "", title)
    title = title.strip("：:、，。.!！?？\"'“”‘’")
    if not title:
        return "自动生成文章"
    if len(title) <= max_chars:
        return title
    return title[:max_chars].rstrip("：:、，。.!！?？\"'“”‘’")


def optimize_article_title(
    row: sqlite3.Row,
    article_context: ArticleContext,
    article_text: str,
    max_output_tokens: int,
) -> TitleOptimizationResult:
    current_title = normalize_title(extract_article_title(article_text) or row["title"])
    feedback = load_title_feedback_summary()
    instructions = """你是今日头条图文标题编辑。
目标是提升自然点击率和完读预期，但必须合规、克制、准确。
只能基于给定材料拟标题，不得编造未出现的事实、数字、人名、因果和结论。
禁止标题党、震惊体、煽动、阴谋论、绝对化承诺。
避免空泛套话：新篇章、新动向、新趋势、有何奥秘、来袭、助力、赋能、引关注、谋发展、显现、开启。
优先把标题落到读者关心的具体点：价格、出行、消费、就业、孩子、旅游、产品变化、普通人影响。
每个标题必须不超过 30 个字符，不能用省略号截断。
请只输出 JSON，不要输出 Markdown。"""
    user_input = f"""请为下面文章生成 5 个今日头条标题候选，并选择 1 个最优标题。

原始 RSS 标题：{row['title']}
当前文章标题：{current_title}
来源：{row['source']}
摘要：{row['summary'] or '无摘要'}
原文抓取状态：{'成功' if article_context.ok else article_context.error}
可核验页面标题：{article_context.title or '无'}

历史标题效果反馈：
{feedback}

文章正文：
{article_text[:3500]}

评分标准：
1. 事实准确，不能超过材料
2. 30 字以内，适合头条标题输入框
3. 让普通人知道“为什么值得点开”
4. 避免空泛、夸张、营销号语气
5. 不要使用“新篇章/新动向/新趋势/有何奥秘/来袭/助力/赋能/引关注”等弱点击套话
6. 若材料没有明确普通人影响，就选择更具体的事实标题，不要硬写宏大判断

输出 JSON 格式：
{{
  "selected": "最终标题",
  "candidates": [
    {{"title": "标题1", "score": 90, "reason": "选择理由"}},
    {{"title": "标题2", "score": 86, "reason": "选择理由"}}
  ]
}}"""
    raw = generate_text(instructions, user_input, max_output_tokens=max_output_tokens)
    data = parse_json_object(raw)
    candidates = parse_title_candidates(data)
    if not candidates:
        raise OpenAIRequestError("title optimization returned no usable candidates")
    selected = normalize_title(str(data.get("selected") or candidates[0].title))
    candidate_titles = {candidate.title for candidate in candidates}
    if selected not in candidate_titles:
        candidates.insert(0, TitleCandidate(selected, 0, "模型选中标题"))
    candidates = dedupe_title_candidates(candidates)[:5]
    selected = choose_title(selected, candidates)
    return TitleOptimizationResult(selected, candidates)


def parse_json_object(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise OpenAIRequestError("title optimization response was not JSON")
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise OpenAIRequestError(f"title optimization JSON parse failed: {exc}") from exc
    if not isinstance(value, dict):
        raise OpenAIRequestError("title optimization JSON was not an object")
    return value


def parse_title_candidates(data: dict) -> list[TitleCandidate]:
    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list):
        return []
    candidates: list[TitleCandidate] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        title = normalize_title(str(item.get("title") or ""))
        if not title or title == "自动生成文章":
            continue
        score_match = re.search(r"\d+", str(item.get("score") or ""))
        score = int(score_match.group(0)) if score_match else 0
        reason = normalize_space(str(item.get("reason") or ""))[:80]
        candidates.append(TitleCandidate(title, score, reason))
    return dedupe_title_candidates(candidates)


def dedupe_title_candidates(candidates: list[TitleCandidate]) -> list[TitleCandidate]:
    seen: set[str] = set()
    result: list[TitleCandidate] = []
    for candidate in candidates:
        key = re.sub(r"\W+", "", candidate.title.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


WEAK_TITLE_PHRASES = [
    "新篇章",
    "新动向",
    "新趋势",
    "有何奥秘",
    "来袭",
    "助力",
    "赋能",
    "引关注",
    "谋发展",
    "显现",
    "开启",
]


def title_has_weak_phrase(title: str) -> bool:
    return any(phrase in title for phrase in WEAK_TITLE_PHRASES)


def choose_title(selected: str, candidates: list[TitleCandidate]) -> str:
    selected = normalize_title(selected)
    usable = [candidate for candidate in candidates if len(candidate.title) <= 30]
    selected_is_usable = any(candidate.title == selected for candidate in usable)
    if selected_is_usable and not title_has_weak_phrase(selected):
        return selected
    if usable:
        return max(
            usable,
            key=lambda candidate: (
                -20 if title_has_weak_phrase(candidate.title) else 0,
                candidate.score,
            ),
        ).title
    return normalize_title(selected)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def load_title_feedback_summary() -> str:
    try:
        return title_feedback_summary(connect_db())
    except Exception as exc:
        return f"暂无可用历史数据（读取失败：{exc}）。"


def write_final_article(
    row: sqlite3.Row,
    article_context: ArticleContext,
    max_output_tokens: int,
    min_article_chars: int,
    optimize_title: bool = True,
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

    title_result = TitleOptimizationResult.from_article_text(text)
    if optimize_title:
        try:
            title_result = optimize_article_title(row, article_context, text, max_output_tokens=900)
            text = replace_article_title(text, title_result.selected)
            print(f"  selected title: {title_result.selected}", file=sys.stderr)
        except OpenAIRequestError as exc:
            print(f"  title optimization skipped: {exc}", file=sys.stderr)
            text = replace_article_title(text, title_result.selected)

    today = article_date_string()
    filename = f"{today}-{row['id']}-{safe_filename(title_result.selected)}.md"
    ARTICLES_DIR.mkdir(exist_ok=True)
    output = ARTICLES_DIR / filename
    title_metadata = title_result.to_markdown()
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

{title_metadata}

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


def write_followup_article(
    candidate: FollowUpCandidate,
    article_context: ArticleContext,
    max_output_tokens: int,
    min_article_chars: int,
    optimize_title: bool = True,
) -> Path:
    row = candidate.row
    detail = json.loads(row["score_detail"])
    text = build_followup_article(
        candidate,
        article_context,
        max_output_tokens,
        min_article_chars,
    )
    if has_placeholder_body(text):
        text = build_followup_article(
            candidate,
            article_context,
            max(max_output_tokens, 3200),
            min_article_chars,
        )
    text = correct_numeric_errors(text, article_context)
    article_len = article_char_count(text)
    expand_attempts = 0
    while article_len < min_article_chars and expand_attempts < 3:
        expand_attempts += 1
        print(
            f"  follow-up article too short: {article_len} chars, expanding to at least {min_article_chars}",
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
            f"generated follow-up article is still too short after expansion: {article_len} < {min_article_chars}"
        )

    title_result = TitleOptimizationResult.from_article_text(text)
    if optimize_title:
        try:
            title_result = optimize_article_title(row, article_context, text, max_output_tokens=900)
            text = replace_article_title(text, title_result.selected)
            print(f"  selected follow-up title: {title_result.selected}", file=sys.stderr)
        except OpenAIRequestError as exc:
            print(f"  title optimization skipped: {exc}", file=sys.stderr)
            text = replace_article_title(text, title_result.selected)

    today = article_date_string()
    filename = f"{today}-{row['id']}-followup-{safe_filename(title_result.selected)}.md"
    ARTICLES_DIR.mkdir(exist_ok=True)
    output = ARTICLES_DIR / filename
    title_metadata = title_result.to_markdown()
    seed = candidate.seed
    content = f"""# 自动追更文章

## 追更来源

- 上一篇文章：{seed.title}
- 上一篇文件：{seed.path}
- 上一篇原始标题：{seed.original_title}
- 上一篇链接：{seed.link or '未知'}
- 新进展标题：{row['title']}
- 新进展来源：{row['source']}
- 新进展链接：{row['link']}
- 新进展最终链接：{article_context.final_url if article_context.final_url != row['link'] else '同上'}
- 新进展抓取：{'成功' if article_context.ok else article_context.error}
- 新进展发布时间：{row['published_at'] or '待核实'}
- 追更相似度：{candidate.similarity:.2f}
- 追更判断：{candidate.reason}
- 选题评分：{row['score']} / 30
- 评分明细：{json.dumps(detail, ensure_ascii=False)}
- 文章长度：{article_len} 字符
- 最低要求：{min_article_chars} 字符

{title_metadata}

## 文章

{text}

## 发布前人工检查

- [ ] 确认这确实是同一事件的新进展
- [ ] 至少核对 2 个可靠来源
- [ ] 删除未证实指控、煽动表达和夸张标题
- [ ] 确认事实、金额、机构名、人名无误
- [ ] 确认没有重复上一篇正文
- [ ] 确认适合今日头条发布
"""
    output.write_text(content, encoding="utf-8")
    return output


def build_followup_article(
    candidate: FollowUpCandidate,
    article_context: ArticleContext,
    max_output_tokens: int,
    min_article_chars: int,
) -> str:
    row = candidate.row
    seed = candidate.seed
    instructions = """你是一个谨慎、平实的中文追更文章编辑，服务于今日头条图文创作者。
账号定位：普通人看懂中外热点背后的影响。
请直接输出完整 Markdown 文章，不要输出创作说明、过程、大纲或占位语。
这是“追更/后续解读”，必须突出新进展，不能简单重复上一篇文章。
必须区分事实与判断；不编造未提供的信息；不确定内容要写“仍需观察”或“需要进一步确认”。
涉及金额、数量、日期、机构名、人名时，必须严格照抄材料中的表达；不要自行换算、扩大单位或改写数字。
避免标题党、煽动性表达、阴谋论、投资建议和医疗法律等专业建议。"""
    article_status = "成功" if article_context.ok else article_context.error
    user_input = f"""请根据以下材料，写一篇不少于 {min_article_chars} 个中文字符的追更文章，建议 1200-1800 字。

上一篇文章标题：{seed.title}
上一篇原始标题：{seed.original_title}
上一篇来源：{seed.source or '未知'}
上一篇链接：{seed.link or '未知'}
上一篇发布时间：{seed.published_at or '未知'}

上一篇正文摘录：
{seed.article_text[:2600]}

这次新进展标题：{row['title']}
来源：{row['source']}
链接：{row['link']}
最终链接：{article_context.final_url}
发布时间：{row['published_at'] or '未知'}
摘要：{row['summary'] or '无摘要'}
本地评分：{row['score']} / 30
评分明细：{row['score_detail']}
追更相似度：{candidate.similarity:.2f}
追更判断：{candidate.reason}
原文抓取状态：{article_status}

可核验页面标题：
{article_context.title or '无'}

可核验页面文本：
{article_context.excerpt(7000) or '无。请明确提醒读者该信息仍需人工补充来源，不要补写具体细节。'}

输出格式：
# 标题
标题体现“新进展”或“后续影响”，不超过 30 字，不能夸张。
不要使用“新篇章、新动向、新趋势、有何奥秘、来袭、助力、赋能、引关注、谋发展、显现、开启”等空泛套话。
优先写清具体变化和普通人影响。

正文结构：
1. 开头：80-120 字说清这次新进展和为什么值得追更
2. 和上一篇相比，新信息是什么：列出材料支持的新事实
3. 为什么它说明事件还在发酵：解释变化、影响或后续连锁反应
4. 对普通人有什么影响：从生活、消费、产业、规则、就业、出行、旅游或产品使用中选择合适角度
5. 接下来关注什么：给 2-3 个观察点
6. 结尾：一句有辨识度的总结

必须直接写完整文章，不要写“此处省略”“请根据大纲撰写”。文章长度必须不小于 {min_article_chars} 个中文字符。"""
    return generate_text(
        instructions=instructions,
        user_input=user_input,
        max_output_tokens=max_output_tokens,
    )


def cmd_publish(args: argparse.Namespace) -> None:
    publish_articles(args.article, args.base_url)


def cmd_metrics_template(args: argparse.Namespace) -> None:
    output = args.output or DATA_DIR / "toutiao_metrics_template.csv"
    print(write_metrics_template(ARTICLES_DIR, output))


def cmd_metrics_import(args: argparse.Namespace) -> None:
    result = import_metrics_csv(
        connect_db(),
        ARTICLES_DIR,
        args.file,
        args.date,
        DATA_DIR / "toutiao_publish_events.jsonl",
    )
    print(f"imported={result.imported} skipped={result.skipped}")
    if result.unmatched:
        print("unmatched titles:")
        for title in result.unmatched:
            print(f"- {title}")


def cmd_metrics_report(args: argparse.Namespace) -> None:
    print(render_title_metrics_report(connect_db(), args.limit))


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
标题要具体、有判断但不夸张。不要使用“新篇章、新动向、新趋势、有何奥秘、来袭、助力、赋能、引关注、谋发展、显现、开启”等空泛套话。
优先写清读者为什么要点开：价格、出行、消费、就业、孩子、旅游、产品变化或普通人影响。

正文结构：
1. 开头：100-150 字说清事件和核心判断
2. 发生了什么：只写材料支持的事实
3. 为什么重要：解释背景和影响
4. 对普通人有什么影响：从消费、就业、生活成本、出行旅游、孩子教育、产品使用或商业规则等角度选择合适方向
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
    auto_parser.add_argument("--category", choices=["international", "china"], help="只生成指定分类")
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
    auto_parser.add_argument(
        "--no-title-optimize",
        dest="title_optimize",
        action="store_false",
        help="不生成 5 个标题候选，直接使用正文原始标题",
    )
    auto_parser.add_argument("--publish-feed", action="store_true", help="生成文章后同步更新 public/feed.xml")
    auto_parser.add_argument("--deploy", action="store_true", help="生成文章后提交并推送到 GitHub Pages")
    auto_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="公网发布根地址")
    auto_parser.add_argument("--commit-message", default="Add generated article", help="自动部署的 git commit 信息")
    auto_parser.set_defaults(
        fetch=True,
        enrich=True,
        require_enriched=True,
        allow_risky=False,
        title_optimize=True,
    )
    auto_parser.set_defaults(func=cmd_auto)

    followup_parser = subparsers.add_parser(
        "followup",
        help="检查已发热点是否发酵，并生成后续解读",
    )
    followup_parser.add_argument("--count", type=int, default=1, help="最多生成追更文章数量")
    followup_parser.add_argument("--lookback-days", type=int, default=5, help="检查最近几天已发文章")
    followup_parser.add_argument("--candidate-limit", type=int, default=120, help="最多检查多少条最新热点")
    followup_parser.add_argument("--min-score", type=int, default=21, help="新进展最低选题评分")
    followup_parser.add_argument("--min-similarity", type=float, default=0.34, help="同一事件最低相似度")
    followup_parser.add_argument("--no-fetch", dest="fetch", action="store_false", help="不先抓取最新热点")
    followup_parser.add_argument("--dry-run", action="store_true", help="只输出追更候选，不生成文章")
    followup_parser.add_argument(
        "--include-followups",
        action="store_true",
        help="允许基于追更文章继续追更，默认只追踪原始文章",
    )
    followup_parser.add_argument(
        "--no-enrich",
        dest="enrich",
        action="store_false",
        help="不抓取新进展原文页面，只使用 RSS 标题和摘要",
    )
    followup_parser.add_argument(
        "--allow-unenriched",
        dest="require_enriched",
        action="store_false",
        help="新进展原文抓取失败时也允许生成追更",
    )
    followup_parser.add_argument("--allow-risky", action="store_true", help="允许战争、冲突、伤亡等高风险追更")
    followup_parser.add_argument("--max-output-tokens", type=int, default=2800)
    followup_parser.add_argument("--min-article-chars", type=int, default=1000, help="追更文章最低字符数")
    followup_parser.add_argument(
        "--no-title-optimize",
        dest="title_optimize",
        action="store_false",
        help="不生成 5 个标题候选，直接使用正文原始标题",
    )
    followup_parser.add_argument("--publish-feed", action="store_true", help="生成追更后同步更新 public/feed.xml")
    followup_parser.add_argument("--deploy", action="store_true", help="生成追更后提交并推送到 GitHub Pages")
    followup_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="公网发布根地址")
    followup_parser.add_argument("--commit-message", default="Add follow-up article", help="自动部署的 git commit 信息")
    followup_parser.set_defaults(
        fetch=True,
        enrich=True,
        require_enriched=True,
        allow_risky=False,
        title_optimize=True,
    )
    followup_parser.set_defaults(func=cmd_followup)

    metrics_template_parser = subparsers.add_parser(
        "metrics-template",
        help="导出头条后台数据回收 CSV 模板",
    )
    metrics_template_parser.add_argument(
        "--output",
        type=Path,
        help="模板输出路径，默认 data/toutiao_metrics_template.csv",
    )
    metrics_template_parser.set_defaults(func=cmd_metrics_template)

    metrics_import_parser = subparsers.add_parser(
        "metrics-import",
        help="导入头条后台 CSV 并计算标题效果分",
    )
    metrics_import_parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="头条后台导出的 CSV 文件",
    )
    metrics_import_parser.add_argument(
        "--date",
        help="CSV 没有日期列时使用的数据日期，例如 2026-07-07",
    )
    metrics_import_parser.set_defaults(func=cmd_metrics_import)

    metrics_report_parser = subparsers.add_parser(
        "metrics-report",
        help="查看标题效果评分报告",
    )
    metrics_report_parser.add_argument("--limit", type=int, default=20)
    metrics_report_parser.set_defaults(func=cmd_metrics_report)

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

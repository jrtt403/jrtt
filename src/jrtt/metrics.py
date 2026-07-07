from __future__ import annotations

import csv
import datetime as dt
import math
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


@dataclass
class ArticleRecord:
    path: Path
    selected_title: str
    original_title: str
    candidates: list[str]


@dataclass
class MetricsImportResult:
    imported: int
    skipped: int
    unmatched: list[str]


TITLE_ALIASES = ["标题", "作品标题", "文章标题", "内容标题", "title"]
DATE_ALIASES = ["日期", "统计日期", "数据日期", "发布时间", "发表时间", "date"]
IMPRESSION_ALIASES = ["展现量", "推荐量", "展示量", "曝光量", "展现", "impressions"]
READ_ALIASES = ["阅读量", "阅读", "点击量", "read_count", "reads", "views"]
CLICK_RATE_ALIASES = ["点击率", "阅读率", "点击阅读率", "ctr", "click_rate"]
FINISH_RATE_ALIASES = ["完读率", "读完率", "completion_rate", "finish_rate"]
AVG_READ_ALIASES = ["平均阅读时长", "平均阅读时间", "avg_read_seconds", "avg_read_time"]
LIKE_ALIASES = ["点赞", "点赞量", "likes"]
COMMENT_ALIASES = ["评论", "评论量", "comments"]
FAVORITE_ALIASES = ["收藏", "收藏量", "favorites"]
SHARE_ALIASES = ["分享", "分享量", "shares"]


def ensure_metrics_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_title_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_path TEXT NOT NULL,
            title TEXT NOT NULL,
            original_title TEXT,
            metric_date TEXT NOT NULL,
            impressions INTEGER NOT NULL DEFAULT 0,
            reads INTEGER NOT NULL DEFAULT 0,
            click_rate REAL,
            finish_rate REAL,
            avg_read_seconds REAL,
            likes INTEGER NOT NULL DEFAULT 0,
            comments INTEGER NOT NULL DEFAULT 0,
            favorites INTEGER NOT NULL DEFAULT 0,
            shares INTEGER NOT NULL DEFAULT 0,
            title_score REAL NOT NULL DEFAULT 0,
            confidence TEXT NOT NULL DEFAULT 'low',
            source_file TEXT,
            imported_at TEXT NOT NULL,
            UNIQUE(article_path, metric_date)
        )
        """
    )
    conn.commit()


def load_article_records(articles_dir: Path) -> list[ArticleRecord]:
    records: list[ArticleRecord] = []
    for path in sorted(articles_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        selected_title = extract_selected_title(raw)
        if not selected_title:
            continue
        records.append(
            ArticleRecord(
                path=path,
                selected_title=selected_title,
                original_title=extract_original_title(raw),
                candidates=extract_title_candidates(raw, selected_title),
            )
        )
    return records


def extract_selected_title(raw: str) -> str:
    selected = re.search(r"^- 选中标题：(.+)$", raw, flags=re.MULTILINE)
    if selected:
        return clean_title(selected.group(1))
    article = section_between(raw, "## 文章", "## 发布前人工检查")
    for line in article.splitlines():
        if line.strip().startswith("# "):
            return clean_title(line.strip()[2:])
    return ""


def extract_original_title(raw: str) -> str:
    match = re.search(r"^- 原标题：(.+)$", raw, flags=re.MULTILINE)
    return clean_title(match.group(1)) if match else ""


def extract_title_candidates(raw: str, selected_title: str) -> list[str]:
    title_section = section_between(raw, "## 标题优化", "## 文章")
    candidates = [selected_title]
    for line in title_section.splitlines():
        match = re.match(r"\s*\d+\.\s*(.+?)(?:（.+)?$", line.strip())
        if match:
            candidates.append(clean_title(match.group(1)))
    return dedupe_titles(candidates)


def section_between(raw: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in raw:
        return ""
    section = raw.split(start_marker, 1)[1]
    if end_marker in section:
        section = section.split(end_marker, 1)[0]
    return section.strip()


def clean_title(value: str) -> str:
    value = re.sub(r"\s+", "", value.strip())
    return value.strip("：:、，。.!！?？\"'“”‘’")


def dedupe_titles(titles: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for title in titles:
        key = normalize_match_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(title)
    return result


def normalize_match_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").lower(), flags=re.UNICODE)


def write_metrics_template(articles_dir: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    records = load_article_records(articles_dir)
    with output.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "标题",
                "日期",
                "展现量",
                "阅读量",
                "点击率",
                "完读率",
                "平均阅读时长",
                "点赞",
                "评论",
                "收藏",
                "分享",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow({"标题": record.selected_title})
    return output


def import_metrics_csv(
    conn: sqlite3.Connection,
    articles_dir: Path,
    csv_path: Path,
    default_date: str | None = None,
) -> MetricsImportResult:
    ensure_metrics_schema(conn)
    records = load_article_records(articles_dir)
    rows = read_csv_rows(csv_path)
    imported = 0
    skipped = 0
    unmatched: list[str] = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    for row in rows:
        title = clean_title(get_field(row, TITLE_ALIASES))
        if not title:
            skipped += 1
            continue
        record = match_article(title, records)
        if record is None:
            unmatched.append(title)
            skipped += 1
            continue

        metric_date = parse_metric_date(get_field(row, DATE_ALIASES), default_date)
        impressions = parse_int(get_field(row, IMPRESSION_ALIASES))
        reads = parse_int(get_field(row, READ_ALIASES))
        click_rate = parse_rate(get_field(row, CLICK_RATE_ALIASES))
        if click_rate is None and impressions:
            click_rate = reads / impressions
        finish_rate = parse_rate(get_field(row, FINISH_RATE_ALIASES))
        avg_read_seconds = parse_seconds(get_field(row, AVG_READ_ALIASES))
        likes = parse_int(get_field(row, LIKE_ALIASES))
        comments = parse_int(get_field(row, COMMENT_ALIASES))
        favorites = parse_int(get_field(row, FAVORITE_ALIASES))
        shares = parse_int(get_field(row, SHARE_ALIASES))
        title_score, confidence = score_title_effect(
            impressions,
            reads,
            click_rate,
            finish_rate,
            likes,
            comments,
            favorites,
            shares,
        )

        conn.execute(
            """
            INSERT INTO article_title_metrics (
                article_path, title, original_title, metric_date, impressions, reads,
                click_rate, finish_rate, avg_read_seconds, likes, comments, favorites,
                shares, title_score, confidence, source_file, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_path, metric_date) DO UPDATE SET
                title = excluded.title,
                original_title = excluded.original_title,
                impressions = excluded.impressions,
                reads = excluded.reads,
                click_rate = excluded.click_rate,
                finish_rate = excluded.finish_rate,
                avg_read_seconds = excluded.avg_read_seconds,
                likes = excluded.likes,
                comments = excluded.comments,
                favorites = excluded.favorites,
                shares = excluded.shares,
                title_score = excluded.title_score,
                confidence = excluded.confidence,
                source_file = excluded.source_file,
                imported_at = excluded.imported_at
            """,
            (
                str(record.path),
                record.selected_title,
                record.original_title,
                metric_date,
                impressions,
                reads,
                click_rate,
                finish_rate,
                avg_read_seconds,
                likes,
                comments,
                favorites,
                shares,
                title_score,
                confidence,
                str(csv_path),
                now,
            ),
        )
        imported += 1
    conn.commit()
    return MetricsImportResult(imported, skipped, unmatched[:20])


def read_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    raw = csv_path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return []
    sample = raw[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(raw.splitlines(), dialect=dialect)
    return [dict(row) for row in reader]


def get_field(row: dict[str, str], aliases: list[str]) -> str:
    normalized = {normalize_header(key): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(normalize_header(alias))
        if value is not None:
            return str(value).strip()
    return ""


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").lower())


def match_article(title: str, records: list[ArticleRecord]) -> ArticleRecord | None:
    key = normalize_match_key(title)
    if not key:
        return None
    for record in records:
        aliases = [record.selected_title, record.original_title, *record.candidates]
        if key in {normalize_match_key(alias) for alias in aliases}:
            return record

    best_record: ArticleRecord | None = None
    best_ratio = 0.0
    for record in records:
        for alias in [record.selected_title, record.original_title, *record.candidates]:
            alias_key = normalize_match_key(alias)
            if not alias_key:
                continue
            ratio = SequenceMatcher(None, key, alias_key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_record = record
    return best_record if best_ratio >= 0.72 else None


def parse_metric_date(value: str, default_date: str | None) -> str:
    if value:
        value = value.strip().replace("/", "-")
        match = re.search(r"\d{4}-\d{1,2}-\d{1,2}", value)
        if match:
            year, month, day = (int(part) for part in match.group(0).split("-"))
            return f"{year:04d}-{month:02d}-{day:02d}"
        match = re.search(r"\d{1,2}-\d{1,2}", value)
        if match:
            year = dt.datetime.now().year
            month, day = (int(part) for part in match.group(0).split("-"))
            return f"{year:04d}-{month:02d}-{day:02d}"
    if default_date:
        return parse_metric_date(default_date, None)
    return dt.datetime.now().strftime("%Y-%m-%d")


def parse_int(value: str) -> int:
    number = parse_number(value)
    return int(round(number)) if number is not None else 0


def parse_number(value: str) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value or value in {"-", "--"}:
        return None
    multiplier = 1.0
    if "万" in value:
        multiplier = 10000.0
    elif "亿" in value:
        multiplier = 100000000.0
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    return float(match.group(0)) * multiplier


def parse_rate(value: str) -> float | None:
    if not value:
        return None
    number = parse_number(value)
    if number is None:
        return None
    if "%" in value or number > 1:
        return number / 100
    return number


def parse_seconds(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d{1,2}:\d{1,2}(?::\d{1,2})?", value):
        parts = [int(part) for part in value.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    minute_match = re.match(r"(?:(\d+)分)?(?:(\d+(?:\.\d+)?)秒)?", value)
    if minute_match and minute_match.group(0):
        minutes = float(minute_match.group(1) or 0)
        seconds = float(minute_match.group(2) or 0)
        return minutes * 60 + seconds
    return parse_number(value)


def score_title_effect(
    impressions: int,
    reads: int,
    click_rate: float | None,
    finish_rate: float | None,
    likes: int,
    comments: int,
    favorites: int,
    shares: int,
) -> tuple[float, str]:
    ctr_pct = max(0.0, (click_rate or 0.0) * 100)
    finish_pct = max(0.0, (finish_rate or 0.0) * 100)
    engagement = likes + comments * 2 + favorites * 2 + shares * 3
    engagement_pct = (engagement / reads * 100) if reads else 0.0
    read_bonus = min(18.0, math.log10(reads + 1) * 4.5)
    score = ctr_pct * 3.0 + finish_pct * 0.35 + engagement_pct * 1.4 + read_bonus
    score = min(100.0, max(0.0, score))
    if impressions >= 10000:
        confidence = "high"
    elif impressions >= 1000:
        confidence = "medium"
    else:
        confidence = "low"
    return round(score, 1), confidence


def render_title_metrics_report(conn: sqlite3.Connection, limit: int = 20) -> str:
    ensure_metrics_schema(conn)
    rows = latest_metric_rows(conn, limit)
    if not rows:
        return "暂无标题数据。先运行 metrics-template，填入头条后台数据后再 metrics-import。"

    lines = ["# 标题效果报告", ""]
    lines.append("## Top 标题")
    for row in rows[:limit]:
        lines.append(
            "- "
            f"{row['title_score']:.1f}分/{row['confidence']} "
            f"阅读{row['reads']} 展现{row['impressions']} "
            f"点击率{format_rate(row['click_rate'])} 完读率{format_rate(row['finish_rate'])}："
            f"{row['title']}"
        )

    lines.extend(["", "## 表现较弱"])
    for row in sorted(rows, key=lambda item: item["title_score"])[: min(5, len(rows))]:
        lines.append(
            "- "
            f"{row['title_score']:.1f}分 "
            f"点击率{format_rate(row['click_rate'])} 阅读{row['reads']}："
            f"{row['title']}"
        )

    feature_lines = render_feature_summary(rows)
    if feature_lines:
        lines.extend(["", "## 标题特征"])
        lines.extend(feature_lines)
    return "\n".join(lines)


def latest_metric_rows(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT m.*
        FROM article_title_metrics m
        JOIN (
            SELECT article_path, MAX(metric_date) AS latest_date
            FROM article_title_metrics
            GROUP BY article_path
        ) latest
          ON latest.article_path = m.article_path
         AND latest.latest_date = m.metric_date
        ORDER BY m.title_score DESC, m.reads DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def format_rate(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.2f}%"


def render_feature_summary(rows: list[sqlite3.Row]) -> list[str]:
    buckets: dict[str, list[sqlite3.Row]] = {
        "12字以内": [],
        "13-18字": [],
        "19-24字": [],
        "25-30字": [],
        "含数字": [],
        "含疑问": [],
        "含背后/影响/信号": [],
    }
    for row in rows:
        title = row["title"]
        length = len(title)
        if length <= 12:
            buckets["12字以内"].append(row)
        elif length <= 18:
            buckets["13-18字"].append(row)
        elif length <= 24:
            buckets["19-24字"].append(row)
        else:
            buckets["25-30字"].append(row)
        if re.search(r"\d", title):
            buckets["含数字"].append(row)
        if any(mark in title for mark in ["?", "？", "为何", "为什么", "怎么"]):
            buckets["含疑问"].append(row)
        if any(word in title for word in ["背后", "影响", "信号"]):
            buckets["含背后/影响/信号"].append(row)

    lines: list[str] = []
    for name, bucket_rows in buckets.items():
        if not bucket_rows:
            continue
        avg_score = sum(float(row["title_score"]) for row in bucket_rows) / len(bucket_rows)
        avg_ctr_values = [float(row["click_rate"]) for row in bucket_rows if row["click_rate"] is not None]
        avg_ctr = sum(avg_ctr_values) / len(avg_ctr_values) if avg_ctr_values else None
        lines.append(f"- {name}：{len(bucket_rows)}篇，均分{avg_score:.1f}，均点击率{format_rate(avg_ctr)}")
    return lines


def title_feedback_summary(conn: sqlite3.Connection, limit: int = 6) -> str:
    ensure_metrics_schema(conn)
    rows = latest_metric_rows(conn, limit * 2)
    if not rows:
        return "暂无历史标题效果数据。"
    strong = rows[:limit]
    weak = sorted(rows, key=lambda item: item["title_score"])[: min(3, len(rows))]
    lines = ["近期高分标题："]
    for row in strong:
        lines.append(
            f"- {row['title']}：{row['title_score']:.1f}分，"
            f"点击率{format_rate(row['click_rate'])}，阅读{row['reads']}"
        )
    lines.append("近期低分标题，避免相似表达：")
    for row in weak:
        lines.append(
            f"- {row['title']}：{row['title_score']:.1f}分，"
            f"点击率{format_rate(row['click_rate'])}，阅读{row['reads']}"
        )
    return "\n".join(lines)

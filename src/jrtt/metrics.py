from __future__ import annotations

import csv
import datetime as dt
import json
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
DATE_ALIASES = ["日期", "统计日期", "数据日期", "date"]
PUBLISH_TIME_ALIASES = ["发布时间", "发表时间", "发布时刻", "publish_time", "published_at"]
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
            publish_time TEXT,
            publish_hour INTEGER,
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
    ensure_column(conn, "article_title_metrics", "publish_time", "TEXT")
    ensure_column(conn, "article_title_metrics", "publish_hour", "INTEGER")
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


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
                "发布时间",
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
    publish_events_path: Path | None = None,
) -> MetricsImportResult:
    ensure_metrics_schema(conn)
    records = load_article_records(articles_dir)
    publish_events = load_publish_events(publish_events_path)
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

        publish_time = (
            parse_publish_time(get_field(row, PUBLISH_TIME_ALIASES))
            or infer_publish_time(record, publish_events)
        )
        metric_date = parse_metric_date(
            get_field(row, DATE_ALIASES),
            default_date or (publish_time.date().isoformat() if publish_time else None),
        )
        publish_time_text = publish_time.isoformat() if publish_time else None
        publish_hour = publish_time.hour if publish_time else None
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
                article_path, title, original_title, metric_date, publish_time, publish_hour, impressions, reads,
                click_rate, finish_rate, avg_read_seconds, likes, comments, favorites,
                shares, title_score, confidence, source_file, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_path, metric_date) DO UPDATE SET
                title = excluded.title,
                original_title = excluded.original_title,
                publish_time = excluded.publish_time,
                publish_hour = excluded.publish_hour,
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
                publish_time_text,
                publish_hour,
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


def load_publish_events(path: Path | None) -> dict[str, dt.datetime]:
    if path is None or not path.exists():
        return {}
    events: dict[str, dt.datetime] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("status") not in {"submitted", "success"}:
            continue
        published_at = parse_publish_time(str(event.get("published_at") or ""))
        if published_at is None:
            continue
        keys = [
            normalize_match_key(str(event.get("article_path") or "")),
            normalize_match_key(str(event.get("article_path_rel") or "")),
            normalize_match_key(str(event.get("title") or "")),
        ]
        for key in keys:
            if key:
                events[key] = published_at
    return events


def infer_publish_time(
    record: ArticleRecord,
    publish_events: dict[str, dt.datetime],
) -> dt.datetime | None:
    keys = [
        normalize_match_key(str(record.path)),
        normalize_match_key(record.path.name),
        normalize_match_key(record.selected_title),
        normalize_match_key(record.original_title),
        *[normalize_match_key(candidate) for candidate in record.candidates],
    ]
    for key in keys:
        if key in publish_events:
            return publish_events[key]
    return None


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


def parse_publish_time(value: str) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    normalized = value.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", normalized):
        normalized = f"{normalized} 00:00:00"
    elif re.fullmatch(r"\d{1,2}:\d{1,2}(?::\d{1,2})?", normalized):
        today = dt.datetime.now().strftime("%Y-%m-%d")
        normalized = f"{today} {normalized}"
    match = re.search(
        r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2})(?::(\d{1,2}))?(?::(\d{1,2}))?)?",
        normalized,
    )
    if not match:
        try:
            return dt.datetime.fromisoformat(normalized)
        except ValueError:
            return None
    year, month, day = (int(match.group(i)) for i in range(1, 4))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    return dt.datetime(year, month, day, hour, minute, second)


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
    lines.extend(["", "## 发布时间建议"])
    lines.extend(render_publish_time_summary(rows))
    lines.extend(["", "## 低表现文章复盘"])
    lines.extend(render_low_performance_review(rows))
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


def render_publish_time_summary(rows: list[sqlite3.Row]) -> list[str]:
    buckets: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        hour = row["publish_hour"]
        if hour is None:
            continue
        buckets.setdefault(int(hour), []).append(row)
    if not buckets:
        return ["- 暂无发布时间数据。CSV 里填“发布时间”，或让自动发布脚本生成本地发布时间日志后再导入。"]

    ranked = sorted(
        buckets.items(),
        key=lambda item: (
            sum(float(row["title_score"]) for row in item[1]) / len(item[1]),
            sum(int(row["reads"]) for row in item[1]) / len(item[1]),
        ),
        reverse=True,
    )
    lines: list[str] = []
    best_hour, best_rows = ranked[0]
    best_avg_score = average([float(row["title_score"]) for row in best_rows])
    lines.append(
        f"- 当前样本最佳时段：{best_hour:02d}:00-{best_hour:02d}:59，"
        f"{len(best_rows)}篇，均分{best_avg_score:.1f}。"
    )
    for hour, bucket_rows in ranked[:8]:
        avg_score = average([float(row["title_score"]) for row in bucket_rows])
        avg_reads = average([float(row["reads"]) for row in bucket_rows])
        avg_ctr = average_optional([row["click_rate"] for row in bucket_rows])
        lines.append(
            f"- {hour:02d}:00：{len(bucket_rows)}篇，"
            f"均分{avg_score:.1f}，均阅读{avg_reads:.0f}，均点击率{format_rate(avg_ctr)}"
        )
    if len(best_rows) < 3:
        lines.append("- 样本少于 3 篇，先作为试验方向，不要立刻固定发布时间。")
    return lines


def render_low_performance_review(rows: list[sqlite3.Row], limit: int = 8) -> list[str]:
    if not rows:
        return ["- 暂无可复盘数据。"]
    weak_rows = sorted(rows, key=lambda row: (float(row["title_score"]), int(row["reads"])))[:limit]
    lines: list[str] = []
    for row in weak_rows:
        diagnosis = diagnose_low_performance(row)
        lines.append(
            "- "
            f"{row['title_score']:.1f}分 阅读{row['reads']} 展现{row['impressions']} "
            f"点击率{format_rate(row['click_rate'])} 完读率{format_rate(row['finish_rate'])}："
            f"{row['title']}；{diagnosis}"
        )
    return lines


def diagnose_low_performance(row: sqlite3.Row) -> str:
    impressions = int(row["impressions"])
    reads = int(row["reads"])
    click_rate = row["click_rate"]
    finish_rate = row["finish_rate"]
    avg_read_seconds = row["avg_read_seconds"]
    engagement = int(row["likes"]) + int(row["comments"]) * 2 + int(row["favorites"]) * 2 + int(row["shares"]) * 3
    engagement_rate = engagement / reads if reads else 0.0
    reasons: list[str] = []
    actions: list[str] = []

    if impressions < 1000:
        reasons.append("展现偏低")
        actions.append("优先复查选题热度、发布时间和账号推荐冷启动")
    if impressions >= 1000 and (click_rate is None or float(click_rate) < 0.03):
        reasons.append("点击率偏低")
        actions.append("标题需要更明确普通人利益点，避免空泛表述")
    if reads >= 100 and finish_rate is not None and float(finish_rate) < 0.25:
        reasons.append("完读率偏低")
        actions.append("开头前80字提前结论，减少背景铺垫")
    if reads >= 100 and avg_read_seconds is not None and float(avg_read_seconds) < 35:
        reasons.append("阅读时长偏短")
        actions.append("首屏结构改成短段落和小标题")
    if reads >= 300 and engagement_rate < 0.005:
        reasons.append("互动偏低")
        actions.append("结尾补一个可讨论的观察点，不做诱导互动")

    if not reasons:
        return "没有明显单项短板，建议观察同题材后续表现"
    return "、".join(reasons) + "；建议：" + "；".join(dedupe_text(actions))


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def average_optional(values: list[object]) -> float | None:
    numeric = [float(value) for value in values if value is not None]
    return average(numeric) if numeric else None


def dedupe_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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

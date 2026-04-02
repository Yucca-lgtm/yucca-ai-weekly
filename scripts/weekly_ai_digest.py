#!/usr/bin/env python3
"""
每周 AI 热点：从 RSS 拉取近 7 天条目 → 抓取页面校验标题 → 一句简体中文摘要 → 推送飞书。
默认完全免费：摘要来自正文/RSS 摘录，经免费公共翻译接口译为简体中文（无需 OpenAI）。
环境变量：
  FEISHU_WEBHOOK    必填
  MYMEMORY_EMAIL      可选：在 https://mymemory.translated.net 登记邮箱可提高 MyMemory 免费额度
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
import feedparser
import httpx
import trafilatura
import yaml
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

# 北京时间用于周报刊头
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

BEIJING = ZoneInfo("Asia/Shanghai") if ZoneInfo else timezone(timedelta(hours=8))

TITLE_MATCH_MIN = 72  # RSS 标题与页面标题相似度阈值（0-100）
MAX_ITEMS = 8
FETCH_TIMEOUT = 25.0
USER_AGENT = (
    "WeeklyAIDigest/1.0 (+https://github.com/actions; compatible; research bot)"
)

# 仅保留与 AI 行业明显相关的条目（综合科技媒体 RSS 会含非 AI 新闻）
_AI_HINTS = (
    "artificial intelligence",
    "machine learning",
    "generative ai",
    "generative artificial",
    "deep learning",
    "large language",
    " llm",
    "llms",
    "openai",
    "anthropic",
    "deepmind",
    "nvidia",
    "chatgpt",
    "gemini",
    "claude",
    "copilot",
    "neural network",
    "transformer",
    "gpt-",
    "hugging face",
    "stability ai",
    "midjourney",
    "diffusion",
    "foundation model",
    "agentic",
    "retrieval-augmented",
    "embedding",
    "人工智能",
    "机器学习",
    "大模型",
    "生成式",
    "多模态",
    "智能体",
    "agent",
    "agentic",
    "workflow",
    "copilot",
    "ai办公",
    "ai 办公",
    "ai助手",
    "ai 助手",
    "办公自动化",
    "效率工具",
    "知识库",
    "rag",
    "智谱",
    "文心",
    "通义",
    "千问",
    "豆包",
    "混元",
    "盘古",
    "昆仑",
    "kimi",
    "月之暗面",
    "minimax",
    "deepseek",
    "深度求索",
    "glm",
    "qwen",
    "ernie",
    "doubao",
    "hunyuan",
    "pangu",
    "百度",
    "阿里",
    "腾讯",
    "字节",
    "火山引擎",
    "华为",
    "小米",
)


def is_ai_related(title: str, summary: str, link: str) -> bool:
    blob = f"{title} {summary} {link}".lower()
    return any(h in blob for h in _AI_HINTS)


def load_feeds(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return list(data.get("feeds") or [])


def parse_entry_date(entry: Any) -> datetime | None:
    if getattr(entry, "published_parsed", None):
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    if getattr(entry, "updated_parsed", None):
        try:
            return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    if getattr(entry, "published", None):
        try:
            dt = parsedate_to_datetime(entry.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    return None


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_title(s: str) -> str:
    s = strip_html(s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    for suffix in (" | openai", " - google deepmind", " - anthropic", " | nvidia"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def fetch_page_title_and_text(url: str) -> tuple[str | None, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()
            raw = r.text
    except Exception:
        return None, ""

    title = None
    soup = BeautifulSoup(raw, "lxml")
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = strip_html(og["content"])
    if not title and soup.title and soup.title.string:
        title = strip_html(soup.title.string)
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = strip_html(h1.get_text(" "))

    try:
        extracted = trafilatura.extract(raw, url=url, include_comments=False) or ""
    except Exception:
        extracted = ""

    return title, extracted


def rss_fallback_summary(entry: Any) -> str:
    summ = getattr(entry, "summary", None) or getattr(entry, "description", "") or ""
    summ = strip_html(summ)
    if len(summ) > 220:
        summ = summ[:220] + "…"
    return summ


def has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def extract_excerpt_sentence(body_text: str, rss_summary: str) -> str:
    """从正文取前两句话级片段，否则用 RSS 摘要，供翻译（仅事实摘录，不杜撰）。"""
    if body_text:
        flat = re.sub(r"\s+", " ", body_text).strip()
        # 英文：按句号/问号分段；中文：按句号
        parts = re.split(r"(?<=[.!?。！？])\s+", flat)
        chunk = ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            chunk = (chunk + " " + p).strip() if chunk else p
            if len(chunk) >= 80:
                break
        if not chunk and parts:
            chunk = parts[0].strip()
        if chunk:
            return chunk[:420]
    return rss_summary[:420] if rss_summary else ""


def translate_mymemory_en_to_zh(text: str) -> str | None:
    """MyMemory 免费接口：https://mymemory.translated.net/doc/usagelimits.php"""
    if not text or len(text.strip()) < 12:
        return None
    q = text[:450]
    params: dict[str, str] = {"q": q, "langpair": "en|zh-CN"}
    email = os.environ.get("MYMEMORY_EMAIL", "").strip()
    if email and "@" in email:
        params["de"] = email
    try:
        with httpx.Client(timeout=45.0, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get("https://api.mymemory.translated.net/get", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None
    tr = (data.get("responseData") or {}).get("translatedText") or ""
    tr = tr.strip()
    if not tr or "MYMEMORY WARNING" in tr.upper():
        return None
    if tr.upper() == q.upper():
        return None
    return tr


def translate_libretranslate_en_to_zh(text: str) -> str | None:
    """公共 LibreTranslate 实例（免费、无 Key；可能偶发不可用，仅作兜底）。"""
    if not text:
        return None
    payload = {"q": text[:450], "source": "en", "target": "zh", "format": "text"}
    for base in (
        "https://libretranslate.de",
        "https://translate.argosopentech.com",
    ):
        try:
            with httpx.Client(timeout=45.0, headers={"User-Agent": USER_AGENT}) as client:
                r = client.post(f"{base}/translate", json=payload)
                r.raise_for_status()
                tr = (r.json() or {}).get("translatedText", "").strip()
            if tr:
                return tr
        except Exception:
            continue
    return None


def excerpt_to_zh_one_line(excerpt: str) -> str:
    """将摘录统一为一句简体中文：已是中文则截断；英文则免费机翻。"""
    excerpt = excerpt.strip()
    if not excerpt:
        return "（暂无可用摘要，请直接阅读原文。）"
    if has_cjk(excerpt):
        one = excerpt.replace("\n", " ").strip()
        if len(one) > 160:
            one = one[:160] + "…"
        return one
    zh = translate_mymemory_en_to_zh(excerpt)
    time.sleep(1.0)
    if not zh:
        zh = translate_libretranslate_en_to_zh(excerpt)
    if zh:
        if len(zh) > 180:
            zh = zh[:180] + "…"
        return zh
    short = excerpt[:200] + ("…" if len(excerpt) > 200 else "")
    return f"【摘要暂以英文呈现】{short}"


def _escape_md(text: str) -> str:
    # 飞书卡片 markdown 对特殊字符较宽容，这里做最基础的清理，避免换行失控。
    return (text or "").replace("\r", "").strip()


def feishu_send_card_zh_cn(
    webhook: str,
    title: str,
    items: list[dict[str, str]] | None = None,
    notice: str | None = None,
) -> None:
    """
    使用飞书消息卡片（schema 2.0）发送，排版更清爽：
    - 标题/每条资讯标题加粗
    - 来源用灰色
    - 每条之间用 divider 分隔，避免拥挤
    """
    elements: list[dict[str, Any]] = []

    if notice:
        elements.append(
            {
                "tag": "markdown",
                "content": _escape_md(notice),
                "text_size": "normal_v2",
            }
        )
    else:
        assert items is not None
        for i, it in enumerate(items, 1):
            t = _escape_md(it.get("title", ""))
            s = _escape_md(it.get("source", ""))
            summ = _escape_md(it.get("summary", ""))
            url = _escape_md(it.get("url", ""))

            # 资讯块：标题（加粗）+ 来源（灰）+ 摘要 + 链接
            md_lines = [f"**{i}. 《{t}》**"]
            if s:
                md_lines.append(f"<font color='grey'>来源：{s}</font>")
            if summ:
                md_lines.append(summ)
            if url:
                md_lines.append(f"[查看原文]({url})")

            elements.append(
                {
                    "tag": "markdown",
                    "content": "\n".join(md_lines),
                    "text_size": "normal_v2",
                    "text_align": "left",
                }
            )

            # 分隔线（最后一条不加）
            if i != len(items):
                elements.append({"tag": "divider"})

    body = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {
                    "text_size": {
                        "normal_v2": {"default": "normal", "pc": "normal", "mobile": "heading"}
                    }
                },
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": elements,
            },
        },
    }

    with httpx.Client(timeout=30.0) as client:
        r = client.post(webhook, json=body)
        r.raise_for_status()
        try:
            data = r.json()
        except json.JSONDecodeError:
            return
        code = data.get("code")
        if code is not None and int(code) != 0:
            raise RuntimeError(f"Feishu API error: {data}")


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(root, "config", "rss_feeds.yaml")
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("FEISHU_WEBHOOK is required", file=sys.stderr)
        return 1

    feeds = load_feeds(config_path)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    candidates: list[dict[str, Any]] = []
    for feed in feeds:
        url = feed.get("url")
        label = feed.get("label", "")
        if not url:
            continue
        try:
            prio = int(feed.get("priority", 50))
        except (TypeError, ValueError):
            prio = 50
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"WARN: feed parse failed {url}: {e}", file=sys.stderr)
            continue
        for entry in getattr(parsed, "entries", []) or []:
            link = getattr(entry, "link", None)
            if not link:
                continue
            dt = parse_entry_date(entry)
            if dt is None or dt < cutoff:
                continue
            title = strip_html(getattr(entry, "title", "") or "")
            rss_sum = rss_fallback_summary(entry)
            if not is_ai_related(title, rss_sum, link):
                continue
            candidates.append(
                {
                    "link": link.strip(),
                    "rss_title": title,
                    "published": dt,
                    "feed_label": label,
                    "priority": prio,
                    "entry": entry,
                }
            )
        time.sleep(0.3)

    # 热点排序：发布时间越新越靠前；同一时间戳下优先「priority」更高的来源（行业权威/配置权重）
    candidates.sort(
        key=lambda x: (x["published"].timestamp(), x["priority"]),
        reverse=True,
    )

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for c in candidates:
        link = c["link"]
        if link in seen:
            continue
        seen.add(link)

        rss_title = c["rss_title"]
        page_title, body_text = fetch_page_title_and_text(link)
        time.sleep(0.5)

        if not page_title:
            print(f"SKIP (no title): {link}", file=sys.stderr)
            continue

        score = fuzz.token_sort_ratio(normalize_title(rss_title), normalize_title(page_title))
        if score < TITLE_MATCH_MIN:
            print(f"SKIP (title mismatch {score}): rss={rss_title!r} page={page_title!r}", file=sys.stderr)
            continue

        display_title = page_title if len(page_title) >= len(rss_title) * 0.5 else rss_title
        rss_sum = rss_fallback_summary(c["entry"])
        raw_excerpt = extract_excerpt_sentence(body_text, rss_sum)
        if not raw_excerpt.strip():
            raw_excerpt = rss_sum
        summ = excerpt_to_zh_one_line(raw_excerpt)

        out.append(
            {
                "title": display_title,
                "summary": summ,
                "url": link,
                "source": c["feed_label"],
            }
        )
        if len(out) >= MAX_ITEMS:
            break

    if ZoneInfo:
        local_now = datetime.now(BEIJING)
    else:
        local_now = datetime.now(timezone.utc) + timedelta(hours=8)

    week_str = local_now.strftime("%Y-%m-%d")
    post_title = f"AI周报 {week_str}"

    if not out:
        notice = "本周未从已配置 RSS 中筛出足够新且标题校验通过的 AI 条目，请检查 config/rss_feeds.yaml 或网络。"
        feishu_send_card_zh_cn(webhook, title=post_title, notice=notice)
        print("Sent empty week notice.")
        return 0

    feishu_send_card_zh_cn(webhook, title=post_title, items=out)
    print(json.dumps({"ok": True, "count": len(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

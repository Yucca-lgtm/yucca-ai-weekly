#!/usr/bin/env python3
"""
每周 AI 热点：从 RSS 拉取近 7 天条目 → 抓取页面校验标题 → 一句简体中文摘要 → 推送飞书。
默认完全免费：摘要来自正文/RSS 摘录，经免费公共翻译接口译为简体中文（无需 OpenAI）。
环境变量：
  FEISHU_WEBHOOK    必填
  MYMEMORY_EMAIL      可选：在 https://mymemory.translated.net 登记邮箱可提高 MyMemory 免费额度
  FEISHU_CARD_TEMPLATE_ID   可选：若设置则用「卡片模板（方案A）」发送（推荐，美观且可复用）
  FEISHU_CARD_TEMPLATE_VERSION  可选：模板版本号（如 1.0.0），不填默认用最新
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import argparse
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin
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
MAX_ITEMS = 5  # 每周五发 5 条精选
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
    "meta",
    "xai",
    "grok",
    "anthropic",
    "google",
    "alphabet",
    "sam altman",
    "奥特曼",
    "黄仁勋",
    "jensen",
    "英伟达",
    "字节跳动",
    "飞书",
    "文心一言",
)


def is_ai_related(title: str, summary: str, link: str) -> bool:
    blob = f"{title} {summary} {link}".lower()
    return any(h in blob for h in _AI_HINTS)


def content_importance_level(title: str, summary: str, link: str) -> int:
    """
    内容重要程度（数字越小越优先）：
    等级一：大模型 / 多模态 / Agent / AI 工具
    等级二：字节、阿里、腾讯、华为、百度、OpenAI、Anthropic、Google、Meta、xAI 等公司的 AI 动作
    等级三：AI 行业动态，以及马斯克、Sam Altman、黄仁勋等关键人物相关 AI 动态

    关键人物仅在标题+摘要中判断，避免链接域名（如 openai.com）把人物稿误判为等级二。
    """
    core = f"{title} {summary}".lower()
    blob = f"{core} {link}".lower()

    l1 = re.search(
        r"(大模型|多模态|multimodal|视觉语言|vlm|vla|"
        r"智能体|agentic|\bagent\b|"
        r"ai工具|ai 工具|工具链|workflow|"
        r"rag|检索增强|知识库|embedding|向量|微调|"
        r"推理|inference|训练|sota|benchmark|"
        r"gpt|claude|gemini|foundation model|llm|large language|"
        r"模型发布|开源模型|computer use|computer-use|tool use|mcp|copilot|插件|"
        r"benchmark|benchmarks)",
        blob,
        re.I,
    )
    if l1:
        return 1

    l3_people = re.search(
        r"(马斯克|musk|黄仁勋|jensen|altman|奥特曼|sam altman|amodei|"
        r"扎克伯格|zuckerberg|梁文锋)",
        core,
        re.I,
    )
    if l3_people:
        return 3

    l2 = re.search(
        r"(字节|bytedance|阿里|alibaba|腾讯|tencent|微信|wechat|"
        r"华为|huawei|鸿蒙|"
        r"百度|baidu|ernie|文心|"
        r"openai|anthropic|google|deepmind|alphabet|gemini|"
        r"meta|xai|grok|"
        r"火山引擎|飞书|钉钉|"
        r"通义|千问|豆包|混元|盘古|kimi|月之暗面|minimax|"
        r"英伟达|nvidia)",
        blob,
        re.I,
    )
    if l2:
        return 2

    l3_ind = re.search(
        r"(行业|市场趋势|趋势|报告|洞察|融资|并购|政策|监管|访谈|年会|展望|市场份额)",
        core,
        re.I,
    )
    if l3_ind:
        return 3

    return 3


_IMPORTANCE_LABEL = {1: "等级一", 2: "等级二", 3: "等级三"}


def classify_item(title: str, summary: str, source: str) -> str:
    """公司动态/模型发布/AI工具/融资合作/人物动态/政策"""
    blob = f"{title} {summary} {source}".lower()
    if re.search(r"(融资|估值|a轮|b轮|c轮|种子轮|天使轮|投资|并购|收购|合作|战略合作|签约)", blob):
        return "融资合作"
    if re.search(r"(政策|监管|条例|征求意见|合规|版权|治理|指引|办法|法案|行政令)", blob):
        return "政策"
    if re.search(
        r"(马斯克|musk|黄仁勋|jensen|altman|奥特曼|sam altman|amodei|nadella|扎克伯格|zuckerberg|梁文锋)",
        blob,
    ):
        return "人物动态"
    if re.search(r"(发布|上线|开源|模型|大模型|多模态|参数|推理|训练|sota|benchmark|gpt|qwen|glm|ernie|pangu|doubao|hunyuan|gemini|claude)", blob):
        return "模型发布"
    if re.search(r"(工具|插件|工作流|agent|智能体|copilot|IDE|办公|文档|表格|ppt|自动化|RAG|知识库)", blob):
        return "AI工具"
    return "公司动态"


def recommend_item(tier: str, title: str, summary: str, popularity: int, published: datetime) -> tuple[bool, str]:
    """返回(是否建议入选, 理由)。理由仅用于候选池/日志，勿写入飞书卡片正文。"""
    cat = classify_item(title, summary, "")
    blob = f"{title} {summary}".lower()
    # 旧闻翻炒：超过 7 天会被上游 cutoff 拦截，这里不再处理
    if tier.upper() == "A":
        return True, f"A 类官方一手来源，归类为「{cat}」。"
    # B 类：需要更“主线/热”
    strong = (
        "智能体",
        "agent",
        "agentic",
        "大模型",
        "多模态",
        "qwen",
        "千问",
        "glm",
        "智谱",
        "豆包",
        "doubao",
        "openai",
        "claude",
        "gemini",
        "nvidia",
        "黄仁勋",
        "马斯克",
        "musk",
        "altman",
        "字节",
        "阿里",
        "腾讯",
        "华为",
        "百度",
        "meta",
        "xai",
        "grok",
        "anthropic",
        "火山引擎",
        "混元",
        "盘古",
        "文心",
        "ernie",
        "ai办公",
        "ai 办公",
    )
    hit = sum(1 for k in strong if k.lower() in blob)
    if hit >= 2:
        return True, f"命中多个主线主题（大模型/多模态/智能体/大厂动态等），归类为「{cat}」。"
    if popularity and popularity >= 20000:
        return True, f"热度指标较高，归类为「{cat}」。"
    if cat in ("模型发布", "融资合作", "政策") and hit >= 1:
        return True, f"高价值类别「{cat}」，且命中主线主题。"
    return False, f"相对偏泛或主线命中不足（类别：{cat}）。"


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


def fetch_page_published(url: str) -> datetime | None:
    """尽量从页面 meta/time 里提取发布时间（UTC）。失败则返回 None。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()
            raw = r.text
    except Exception:
        return None

    soup = BeautifulSoup(raw, "lxml")
    for key in ("article:published_time", "og:published_time"):
        m = soup.find("meta", property=key)
        if m and m.get("content"):
            try:
                dt = datetime.fromisoformat(m["content"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    # 常见 time 标签
    t = soup.find("time")
    if t:
        for attr in ("datetime", "content"):
            v = t.get(attr)
            if v:
                try:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc)
                except Exception:
                    pass
        txt = strip_html(t.get_text(" "))
        # 兜底：2026-04-02 / 2026/04/02
        m = re.search(r"(20\\d{2})[-/](\\d{1,2})[-/](\\d{1,2})", txt)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return datetime(y, mo, d, tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def collect_aibase_news(url: str, limit: int = 30) -> list[dict[str, Any]]:
    """抓取 AIbase 列表页（不依赖 RSS）。列表顺序作为弱热度信号。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()
            raw = r.text
    except Exception:
        return []

    soup = BeautifulSoup(raw, "lxml")
    out: list[dict[str, Any]] = []
    # 页面结构：### 标题 + [摘要](链接)
    for h in soup.find_all(["h3", "h4"]):
        title = strip_html(h.get_text(" "))
        if not title:
            continue
        a = h.find_next("a")
        if not a or not a.get("href"):
            continue
        link = a["href"].strip()
        if not link.startswith("http"):
            link = "https://www.aibase.com" + link
        summary = strip_html(a.get_text(" "))
        out.append({"rss_title": title, "link": link, "rss_summary": summary})
        if len(out) >= limit:
            break
    # 弱热度：越靠前越热
    for idx, it in enumerate(out):
        it["popularity"] = max(0, limit - idx)
    return out


def collect_openai_zh_news(url: str, limit: int = 40) -> list[dict[str, Any]]:
    """抓取 OpenAI 中文资讯列表页（不依赖 RSS）。若遇 Cloudflare 挑战则返回空列表。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()
            raw = r.text
    except Exception:
        return []

    if "Just a moment" in raw or "cf-challenge" in raw.lower() or "_cf_chl_opt" in raw:
        return []

    soup = BeautifulSoup(raw, "lxml")
    base = "https://openai.com"
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href.startswith("http"):
            href = urljoin(base, href)
        if "openai.com" not in href:
            continue
        if "/zh-Hans-CN/" not in href:
            continue
        if href.rstrip("/") == url.rstrip("/"):
            continue
        if re.search(r"/zh-Hans-CN/news/?$", href):
            continue
        title = strip_html(a.get_text(" "))
        if len(title) < 6:
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({"rss_title": title, "link": href, "rss_summary": ""})
        if len(out) >= limit:
            break
    for idx, it in enumerate(out):
        it["popularity"] = max(0, limit - idx)
    return out


def collect_hot36kr(url: str, limit: int = 30) -> list[dict[str, Any]]:
    """36氪热榜聚合接口（含 statRead 等字段）。"""
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []

    # 兼容不同字段结构：优先 data 列表，否则尝试 result/list
    items = data.get("data") or data.get("result") or data.get("list") or []
    out: list[dict[str, Any]] = []
    for it in items:
        title = strip_html(it.get("widgetTitle") or it.get("title") or "")
        link = it.get("url") or it.get("link") or it.get("itemUrl") or ""
        if not title or not link:
            continue
        if link.startswith("//"):
            link = "https:" + link
        if not link.startswith("http"):
            continue
        # 阅读数作为热度（兜底用排序）
        pop = it.get("statRead") or it.get("read") or it.get("hot") or 0
        try:
            pop = int(pop)
        except Exception:
            pop = 0
        out.append({"rss_title": title, "link": link, "rss_summary": "", "popularity": pop})
        if len(out) >= limit:
            break
    # 如果没有阅读数，用排序做弱热度
    if out and all((x.get("popularity") or 0) == 0 for x in out):
        for idx, it in enumerate(out):
            it["popularity"] = max(0, limit - idx)
    return out


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


def _build_items_markdown(items: list[dict[str, str]]) -> str:
    """
    生成用于卡片模板变量的 markdown 字符串（更紧凑、好看）。
    注意：颜色使用 <font> 属于卡片 markdown 的能力；不同端表现可能略有差异。
    """
    blocks: list[str] = []
    for i, it in enumerate(items, 1):
        t = _escape_md(it.get("title", ""))
        s = _escape_md(it.get("source", ""))
        summ = _escape_md(it.get("summary", ""))
        url = _escape_md(it.get("url", ""))

        lines: list[str] = [f"**{i}. 《{t}》**"]
        if s:
            lines.append(f"<font color='grey'>来源：{s}</font>")
        if summ:
            lines.append(summ)
        if url:
            lines.append(f"[查看原文]({url})")
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)


def feishu_send_card_template_zh_cn(
    webhook: str,
    template_id: str,
    template_version: str | None,
    template_variable: dict[str, Any],
) -> None:
    """
    方案A：使用飞书「卡片模板」发送（CardKit 发布的 template_id）。
    参考官方文档：使用自定义机器人发送飞书卡片
    """
    data: dict[str, Any] = {
        "template_id": template_id,
        "template_variable": template_variable,
    }
    if template_version:
        data["template_version_name"] = template_version

    body = {"msg_type": "interactive", "card": {"type": "template", "data": data}}

    with httpx.Client(timeout=30.0) as client:
        r = client.post(webhook, json=body)
        r.raise_for_status()
        try:
            resp = r.json()
        except json.JSONDecodeError:
            return
        code = resp.get("code")
        if code is not None and int(code) != 0:
            raise RuntimeError(f"Feishu API error: {resp}")


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
                # 飞书卡片 schema 2.0 分割线组件 tag 为 hr
                elements.append({"tag": "hr"})

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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["weekly", "candidates"],
        default="weekly",
        help="weekly: 选 5 条并发飞书；candidates: 仅输出候选池 20 条到日志/附件",
    )
    args = parser.parse_args()

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
        tier = str(feed.get("tier") or "B").strip().upper()
        if not url:
            continue
        try:
            prio = int(feed.get("priority", 50))
        except (TypeError, ValueError):
            prio = 50
        kind = str(feed.get("kind") or "rss").strip().lower()

        if kind == "aibase_html":
            for it in collect_aibase_news(url, limit=40):
                link = it["link"]
                title = it["rss_title"]
                rss_sum = it.get("rss_summary", "")
                if not is_ai_related(title, rss_sum, link):
                    continue
                # AIbase 列表页没有稳定发布时间字段：尝试从文章页提取；失败则当作“本周”弱信号
                dt = fetch_page_published(link) or now
                if dt < cutoff:
                    continue
                candidates.append(
                    {
                        "link": link,
                        "rss_title": title,
                        "published": dt,
                        "feed_label": label,
                        "tier": tier,
                        "priority": prio,
                        "popularity": int(it.get("popularity") or 0),
                        "entry": None,
                    }
                )
                time.sleep(0.3)
            continue

        if kind == "hot36kr_api":
            for it in collect_hot36kr(url, limit=40):
                link = it["link"]
                title = it["rss_title"]
                rss_sum = it.get("rss_summary", "")
                if not is_ai_related(title, rss_sum, link):
                    continue
                dt = fetch_page_published(link) or now
                if dt < cutoff:
                    continue
                candidates.append(
                    {
                        "link": link,
                        "rss_title": title,
                        "published": dt,
                        "feed_label": label,
                        "tier": tier,
                        "priority": prio,
                        "popularity": int(it.get("popularity") or 0),
                        "entry": None,
                    }
                )
                time.sleep(0.3)
            continue

        if kind == "openai_zh_html":
            for it in collect_openai_zh_news(url, limit=40):
                link = it["link"]
                title = it["rss_title"]
                rss_sum = it.get("rss_summary", "")
                if not is_ai_related(title, rss_sum, link):
                    continue
                dt = fetch_page_published(link) or now
                if dt < cutoff:
                    continue
                candidates.append(
                    {
                        "link": link,
                        "rss_title": title,
                        "published": dt,
                        "feed_label": label,
                        "tier": tier,
                        "priority": prio,
                        "popularity": int(it.get("popularity") or 0),
                        "entry": None,
                    }
                )
                time.sleep(0.3)
            continue

        # 默认 RSS
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
                    "tier": tier,
                    "priority": prio,
                    "popularity": 0,
                    "entry": entry,
                }
            )
        time.sleep(0.3)

    # 热点排序：发布时间（新） + A类优先 + 来源权重 + 热度（阅读/榜单/位置） + 主题关键词命中
    def _kw_bonus(title: str, summary: str, link: str) -> int:
        blob = f"{title} {summary} {link}".lower()
        # 你关心的高权重主题
        strong = (
            "智能体",
            "agent",
            "agentic",
            "大模型",
            "多模态",
            "qwen",
            "千问",
            "glm",
            "智谱",
            "豆包",
            "doubao",
            "openai",
            "claude",
            "gemini",
            "nvidia",
            "黄仁勋",
            "马斯克",
            "musk",
            "altman",
            "字节",
            "阿里",
            "腾讯",
            "华为",
            "百度",
            "meta",
            "xai",
            "grok",
            "anthropic",
            "火山引擎",
            "混元",
            "盘古",
            "文心",
            "ernie",
            "ai办公",
            "ai 办公",
            "workbuddy",
            "copilot",
        )
        bonus = 0
        for k in strong:
            if k.lower() in blob:
                bonus += 8
        return bonus

    def _score(c: dict[str, Any]) -> float:
        recency = c["published"].timestamp()
        pr = float(c.get("priority") or 0)
        pop = float(c.get("popularity") or 0)
        tier_bonus = 50_000.0 if str(c.get("tier") or "").upper() == "A" else 0.0
        # 让 pop 不至于把“新消息”完全碾压：做 log 压缩
        pop_adj = 0.0
        if pop > 0:
            pop_adj = 20.0 * (1.0 + (pop ** 0.5) / 100.0)
        kw = float(_kw_bonus(c["rss_title"], rss_fallback_summary(c["entry"]) if c.get("entry") else "", c["link"]))
        rss_sum = rss_fallback_summary(c["entry"]) if c.get("entry") else ""
        imp = content_importance_level(c["rss_title"], rss_sum, c["link"])
        # 等级一 > 二 > 三，权重远大于单条时间差（秒级）
        imp_bonus = float(4 - imp) * 1e12
        return imp_bonus + recency + tier_bonus + pr * 1000.0 + pop_adj * 1000.0 + kw * 1000.0

    candidates.sort(key=_score, reverse=True)

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    candidate_rows: list[dict[str, Any]] = []
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
        rss_sum = rss_fallback_summary(c["entry"]) if c.get("entry") else ""
        raw_excerpt = extract_excerpt_sentence(body_text, rss_sum)
        if not raw_excerpt.strip():
            raw_excerpt = rss_sum
        summ = excerpt_to_zh_one_line(raw_excerpt)

        cat = classify_item(display_title, summ, c["feed_label"])
        rec, reason = recommend_item(str(c.get("tier") or "B"), display_title, summ, int(c.get("popularity") or 0), c["published"])
        imp = content_importance_level(display_title, summ, link)
        po = len(candidate_rows)
        candidate_rows.append(
            {
                "title": display_title,
                "time": c["published"].astimezone(BEIJING).strftime("%Y-%m-%d %H:%M"),
                "source": c["feed_label"],
                "tier": c.get("tier") or "B",
                "category": cat,
                "importance_level": imp,
                "importance_label": _IMPORTANCE_LABEL.get(imp, "等级三"),
                "recommended": "是" if rec else "否",
                "reason": reason,
                "one_line": summ,
                "url": link,
                "popularity": int(c.get("popularity") or 0),
                "pool_order": po,
            }
        )

        # 先堆 20 条候选池
        if len(candidate_rows) >= 20:
            break

    # 按「重要程度」优先，同级保持抓取顺序（已由上游 _score 大致保证新优先）
    candidate_rows.sort(key=lambda x: (x["importance_level"], x["pool_order"]))

    # 候选池模式：仅输出候选池 20 条，不发飞书
    if args.mode == "candidates":
        # 输出到 Actions 日志（简洁）+ 生成附件文件
        out_dir = os.path.join(root, "out")
        os.makedirs(out_dir, exist_ok=True)
        json_path = os.path.join(out_dir, "candidates.json")
        md_path = os.path.join(out_dir, "candidates.md")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(candidate_rows, f, ensure_ascii=False, indent=2)
        md_lines = [f"# 候选池（近7天）共 {len(candidate_rows)} 条", ""]
        for i, it in enumerate(candidate_rows, 1):
            md_lines.append(f"## {i}. {it['title']}")
            md_lines.append(f"- 时间：{it['time']}")
            md_lines.append(f"- 来源：{it['source']}（{it['tier']}）")
            md_lines.append(f"- 分类：{it['category']}")
            md_lines.append(f"- 重要程度：{it.get('importance_label', '')}（{it.get('importance_level', '')}）")
            md_lines.append(f"- 建议入选：{it['recommended']}")
            md_lines.append(f"- 理由：{it['reason']}")
            md_lines.append(f"- 链接：{it['url']}")
            md_lines.append("")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines).strip() + "\n")
        print(json.dumps({"mode": "candidates", "count": len(candidate_rows), "json": "out/candidates.json", "md": "out/candidates.md"}, ensure_ascii=False))
        return 0

    # weekly 模式：从候选池里精选 5 条（同等级内按 pool_order）
    picked = [x for x in candidate_rows if x["recommended"] == "是"]
    picked.sort(key=lambda x: (x["importance_level"], x["pool_order"]))
    # 不足 5 条则用剩余候选按重要程度递补
    if len(picked) < MAX_ITEMS:
        remaining = [x for x in candidate_rows if x["recommended"] == "否"]
        remaining.sort(key=lambda x: (x["importance_level"], x["pool_order"]))
        picked.extend(remaining[: MAX_ITEMS - len(picked)])
    picked = picked[:MAX_ITEMS]
    for it in picked:
        one = (it.get("one_line") or "").strip()
        card_summary = f"{it['category']}｜{one}" if one else it["category"]
        out.append({"title": it["title"], "summary": card_summary, "url": it["url"], "source": it["source"]})

    if ZoneInfo:
        local_now = datetime.now(BEIJING)
    else:
        local_now = datetime.now(timezone.utc) + timedelta(hours=8)

    week_str = local_now.strftime("%Y-%m-%d")
    post_title = f"AI周报 {week_str}"
    template_id = os.environ.get("FEISHU_CARD_TEMPLATE_ID", "").strip()
    template_version = os.environ.get("FEISHU_CARD_TEMPLATE_VERSION", "").strip() or None
    print(
        json.dumps(
            {
                "send_mode": "template" if template_id else "fallback_card",
                "template_id_set": bool(template_id),
                "template_version": template_version or "",
            },
            ensure_ascii=False,
        )
    )

    if not out:
        notice = "本周未从已配置 RSS 中筛出足够新且标题校验通过的 AI 条目，请检查 config/rss_feeds.yaml 或网络。"
        if template_id:
            feishu_send_card_template_zh_cn(
                webhook,
                template_id=template_id,
                template_version=template_version,
                template_variable={"date": week_str, "title": post_title, "notice": notice},
            )
        else:
            feishu_send_card_zh_cn(webhook, title=post_title, notice=notice)
        print("Sent empty week notice.")
        return 0

    if template_id:
        items_md = _build_items_markdown(out)
        feishu_send_card_template_zh_cn(
            webhook,
            template_id=template_id,
            template_version=template_version,
            template_variable={"date": week_str, "title": post_title, "items_md": items_md},
        )
    else:
        feishu_send_card_zh_cn(webhook, title=post_title, items=out)
    print(json.dumps({"ok": True, "count": len(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

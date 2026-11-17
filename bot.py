"""
Forum Thread Finder Bot (Telegram, for Railway).

A generic XenForo forum search agent. You send a term
(a person's name / topic) -> the bot logs into the configured
XenForo-based community site, runs the board's keyword search,
crawls the matching thread(s) page by page, and replies with a
structured report of all outbound content-hosting and social links.

Built on the same login + crawl techniques used by well-known open-
source forum scrapers: XenForo CSRF/login flow, `rel="next"`
pagination following, per-thread link extraction and classification.

The target site is fully configurable via env vars — no explicit
site name or branding is hardcoded anywhere in this project.

Environment variables:
  TELEGRAM_BOT_TOKEN   - token from @BotFather (required)
  FORUM_USERNAME       - forum account username (required)
  FORUM_PASSWORD       - forum account password (required)
  ALLOWED_CHAT_IDS     - optional comma-separated chat id whitelist
  FORUM_BASE_URL      - required: base URL of the XenForo board to search
"""

from __future__ import annotations

import asyncio
import html as html_esc
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple

import requests
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging / config
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# keep PTB's own httpx logs quiet
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("forum-finder-bot")


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.error("Missing required environment variable: %s", name)
        raise SystemExit(f"Missing required environment variable: {name}")
    return val


BASE_URL = os.environ.get("FORUM_BASE_URL", "").rstrip("/")
if not BASE_URL:
    log.error(
        "FORUM_BASE_URL env var is required (e.g. 'https://your-xenforo-board.example'). "
        "No default site is hardcoded in this project."
    )
    raise SystemExit("Missing required environment variable: FORUM_BASE_URL")
DOMAIN = re.sub(r"^https?://", "", BASE_URL)

TELEGRAM_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
FORUM_USERNAME = _require_env("FORUM_USERNAME")
FORUM_PASSWORD = _require_env("FORUM_PASSWORD")

ALLOWED_CHAT_IDS: Set[int] = {
    int(x)
    for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0"
)

# How many search hits get fully crawled
MAX_THREADS_TO_CRAWL = 3
# Hard safety cap on pages crawled per thread
CRAWL_PAGE_LIMIT = 50
# Politeness delay between forum requests (seconds)
REQUEST_DELAY = 1.2
# Caps for the report so we never blow up Telegram limits
MAX_FILE_LINKS = 150
MAX_IMAGE_LINKS = 30
MAX_SOCIAL_LINKS = 40
REQUEST_TIMEOUT = 20

URL_RE = re.compile(r'https?://[^\s"\'<>]+')
TAG_RE = re.compile(r"<[^>]+>")
ASSET_EXT_RE = re.compile(
    r"\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot)([?#]|$)", re.I
)

# ---------------------------------------------------------------------------
# Link classification
# ---------------------------------------------------------------------------

FILE_HOSTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"mega\.(?:nz|co\.nz)", re.I), "Mega"),
    (re.compile(r"bunkr[a-z]*\.[a-z]+", re.I), "Bunkr"),
    (re.compile(r"cyber(?:drop|file)[a-z]*\.[a-z]+", re.I), "Cyberdrop"),
    (re.compile(r"gofile\.io", re.I), "Gofile"),
    (re.compile(r"pixeldrain\.com", re.I), "Pixeldrain"),
    (re.compile(r"erome\.com", re.I), "Erome"),
    (re.compile(r"lovefap\.com", re.I), "Lovefap"),
    (re.compile(r"streamtape|streamlare|voe|vid(?:e|s|eo)?\.?to|sendvid|xvideos", re.I), "Video host"),
    (re.compile(r"anonfiles|gofile|hexupload|highload", re.I), "File host"),
]

IMAGE_HOSTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"imgbox\.com", re.I), "Imgbox"),
    (re.compile(r"pixhost\.to", re.I), "Pixhost"),
    (re.compile(r"image(?:haha|twist|bam)", re.I), "Image host"),
    (re.compile(r"i(?:bb|b)\.co", re.I), "Imgbb"),
    (re.compile(r"jpgchurch|jpg\.fyi", re.I), "JPG-Church"),
    (re.compile(r"pixl", re.I), "Pixl"),
]

SOCIAL_HOSTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"onlyfans\.com", re.I), "OnlyFans"),
    (re.compile(r"fansly\.com", re.I), "Fansly"),
    (re.compile(r"(?:twitter\.com|(?:^|//|\.)x\.com)", re.I), "Twitter/X"),
    (re.compile(r"instagram\.com", re.I), "Instagram"),
    (re.compile(r"reddit\.com", re.I), "Reddit"),
    (re.compile(r"patreon\.com", re.I), "Patreon"),
    (re.compile(r"tiktok\.com", re.I), "TikTok"),
    (re.compile(r"t\.me|telegram", re.I), "Telegram"),
    (re.compile(r"discord", re.I), "Discord"),
]

JUNK_HOST_RE = re.compile(
    r"(google|apple|microsoft|paypal|cloudflare|jsdelivr|jquery|"
    r"fonts|gravatar|schema\.org|w3\.org|adobe)",
    re.I,
)


def classify(url: str) -> Optional[Tuple[str, str]]:
    """Return (category, label) for a URL, or None if uninteresting."""
    if DOMAIN in url:
        return None  # internal forum link
    if ASSET_EXT_RE.search(url):
        return None
    if JUNK_HOST_RE.search(url):
        return None
    for pat, label in FILE_HOSTS:
        if pat.search(url):
            return ("file", label)
    for pat, label in SOCIAL_HOSTS:
        if pat.search(url):
            return ("social", label)
    for pat, label in IMAGE_HOSTS:
        if pat.search(url):
            return ("image", label)
    return None


# ---------------------------------------------------------------------------
# Forum session (XenForo login, same CSRF flow used by the `lols` scraper)
# ---------------------------------------------------------------------------


class ForumSession:
    """A logged-in requests.Session against a XenForo-based board."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.8",
                "Referer": BASE_URL + "/",
            }
        )
        self._login()

    # -- request helpers ---------------------------------------------------

    def get(self, path_or_url: str, **kw) -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        kw.setdefault("timeout", REQUEST_TIMEOUT)
        r = self.session.get(url, **kw)
        time.sleep(REQUEST_DELAY)
        return r

    def post(self, path_or_url: str, **kw) -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        kw.setdefault("timeout", REQUEST_TIMEOUT)
        r = self.session.post(url, **kw)
        time.sleep(REQUEST_DELAY)
        return r

    # -- login flow (mirrors the lols XenForo auth mixin) ----------------------------

    def _login(self) -> None:
        log.info("Logging into %s ...", DOMAIN)

        # Step 1: CSRF token from the landing page.
        r = self.get("/")
        m = re.search(r'data-csrf="([^"]+)"', r.text)
        if not m:
            raise RuntimeError(f"Could not find CSRF token on {BASE_URL}/")
        csrf = m.group(1)

        # Step 2: pre-login XHR page gives the login form's _xfToken.
        xf_token = None
        try:
            r = self.get(
                "/login/",
                params={
                    "_xfRequestUri": "/",
                    "_xfWithData": "1",
                    "_xfToken": csrf,
                    "_xfResponseType": "json",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            html = r.json()["html"]["content"]
            m = re.search(r'name="_xfToken" value="([^"]+)"', html)
            if m:
                xf_token = m.group(1)
        except Exception as e:  # noqa: BLE001
            log.warning("Pre-login step failed (%s), falling back...", e)

        # Fallback: plain login page HTML.
        if not xf_token:
            r = self.get("/login/")
            m = re.search(r'name="_xfToken" value="([^"]+)"', r.text)
            if m:
                xf_token = m.group(1)
        if not xf_token:
            raise RuntimeError("Could not extract _xfToken from login page.")

        # Step 3: submit the credentials.
        r = self.post(
            "/login/login",
            data={
                "login": self.username,
                "password": self.password,
                "remember": "1",
                "_xfRedirect": BASE_URL + "/",
                "_xfToken": xf_token,
            },
        )

        cookies = dict(self.session.cookies)
        missing = [c for c in ("xf_csrf", "xf_session", "xf_user") if not cookies.get(c)]
        if missing:
            raise RuntimeError(
                "Forum login failed (missing cookies: %s). "
                "Check FORUM_USERNAME / FORUM_PASSWORD." % missing
            )
        log.info("Successfully logged into %s.", DOMAIN)


# ---------------------------------------------------------------------------
# Search & crawl
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    title: str
    url: str


@dataclass
class ThreadInfo:
    title: str
    url: str
    pages: int = 0
    file_links: List[str] = field(default_factory=list)
    image_links: List[str] = field(default_factory=list)
    social_links: List[str] = field(default_factory=list)

    @property
    def total_links(self) -> int:
        return len(self.file_links) + len(self.image_links) + len(self.social_links)


def _strip_tags(s: str) -> str:
    return TAG_RE.sub("", s).strip()


def _abs_url(href: str) -> str:
    href = html_esc.unescape(href).strip().rstrip('.,);"\'')
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return BASE_URL + href
    return href


def _thread_key(url: str) -> str:
    """Numeric thread id from a XenForo url (/threads/slug.12345/)."""
    m = re.search(r"/(?:threads|posts)/(?:[^/?#]*?\.)?(\d+)/?", url)
    return m.group(1) if m else url


def search_forum(sc: ForumSession, query: str) -> List[SearchResult]:
    """Keyword search on the forum (XenForo search, relevance order)."""
    r = sc.get("/search/search", params={"keywords": query, "o": "relevance"})
    if r.status_code == 429:
        time.sleep(10)
        r = sc.get("/search/search", params={"keywords": query, "o": "relevance"})
    if r.status_code != 200:
        raise RuntimeError(f"Search failed: HTTP {r.status_code}")

    html = r.text
    results: List[SearchResult] = []
    seen: Set[str] = set()

    # XenForo search results put the title in an <h3><a href=...> block.
    matches = re.findall(
        r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', html
    )
    if not matches:  # layout fallback: any thread/post link
        matches = re.findall(r'<a[^>]+href="([^"]*(?:/threads/|/posts/)[^"]+)"[^>]*>([\s\S]*?)</a>', html)

    for href, title in matches:
        url = _abs_url(href)
        if DOMAIN not in url or not re.search(r"/(threads|posts)/", url):
            continue
        key = _thread_key(url)
        if key in seen:
            continue
        title = _strip_tags(html_esc.unescape(title))[:120] or "(untitled)"
        seen.add(key)
        results.append(SearchResult(title, url))

    # Prefer exact-title matches, keep the rest as loose hits.
    q = query.lower().strip()
    exact = [x for x in results if q in x.title.lower()]
    loose = [x for x in results if x not in exact]
    return exact + loose


def crawl_thread(
    sc: ForumSession, thread_url: str, progress: Optional[Callable[[int], None]] = None
) -> ThreadInfo:
    """Crawl all pages of a thread (follows rel=next, like lols)."""
    info = ThreadInfo(title="", url=thread_url)
    url: Optional[str] = thread_url
    pages = 0

    while url and pages < CRAWL_PAGE_LIMIT:
        r = sc.get(url)
        if r.status_code != 200:
            raise RuntimeError(f"Failed fetching {url}: HTTP {r.status_code}")
        html = r.text
        pages += 1

        if not info.title:
            t = re.search(r"<title>([\s\S]*?)</title>", html, re.I)
            if t:
                info.title = html_esc.unescape(_strip_tags(t.group(1))).strip()

        # Collect links from the raw page (covers hrefs, img srcs and
        # plain-text links inside posts/spoilers).
        for raw in URL_RE.findall(html):
            link = html_esc.unescape(raw).strip().rstrip('.,);"\'')
            cat = classify(link)
            if cat is None:
                continue
            category, label = cat
            bucket = {
                "file": info.file_links,
                "image": info.image_links,
                "social": info.social_links,
            }[category]
            if link not in bucket:
                bucket.append(link)

        if progress:
            progress(pages)

        # Follow rel=next pagination (same approach as lols' crawler).
        nxt = re.findall(r'rel="next"\s+href="([^"]+)"', html) or re.findall(
            r'href="([^"]+)"\s+rel="next"', html
        )
        if nxt:
            url = _abs_url(nxt[0])
        else:
            url = None

    info.pages = pages
    return info


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------


def build_report(query: str, threads: List[ThreadInfo], errors: List[str]) -> str:
    """Plain-text report (used for the .txt attachment)."""
    lines = [
        f"Forum search ({DOMAIN}): {query}",
        f"Matching threads crawled: {len(threads)}",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
    ]
    for t in threads:
        lines += [f"THREAD: {t.title}", f"URL: {t.url}", f"Pages scanned: {t.pages}"]
        if t.file_links:
            lines.append(f"  -- Content links ({len(t.file_links)}) --")
            lines += [f"  {u}" for u in t.file_links]
        if t.social_links:
            lines.append(f"  -- Social links ({len(t.social_links)}) --")
            lines += [f"  {u}" for u in t.social_links]
        if t.image_links:
            lines.append(f"  -- Image hosts ({len(t.image_links)}) --")
            lines += [f"  {u}" for u in t.image_links]
        lines += ["-" * 60, ""]
    if errors:
        lines += ["Errors:", *[f"  ! {e}" for e in errors]]
    return "\n".join(lines)


def build_html_chunks(query: str, threads: List[ThreadInfo], errors: List[str]) -> List[str]:
    """HTML chunks for inline Telegram replies (max ~3800 chars each)."""

    def link_line(u: str, label: str) -> str:
        return f'• <a href="{html_esc.escape(u)}">{html_esc.escape(label)}</a>'

    parts: List[str] = []
    cur: List[str] = []

    def push(line: str) -> None:
        cur.append(line)
        if len("\n".join(cur)) > 3800:
            parts.append("\n".join(cur))
            cur.clear()

    total_file = sum(len(t.file_links) for t in threads)
    total_img = sum(len(t.image_links) for t in threads)
    total_soc = sum(len(t.social_links) for t in threads)
    total_pages = sum(t.pages for t in threads)

    push(f"🔎 <b>Forum results for “{html_esc.escape(query)}”</b>")
    push(f"🧵 {len(threads)} thread(s) · 📄 {total_pages} pages scanned")
    push(f"📁 {total_file} content · 🌐 {total_soc} social · 🖼 {total_img} image links")

    for t in threads:
        if not t.total_links:
            continue
        push("")
        push(f"<b>━━ {html_esc.escape(t.title[:80])}</b> ({t.pages} pages)")
        if t.file_links:
            push(f"<b>📁 Content links ({len(t.file_links)}):</b>")
            for u in t.file_links[:MAX_FILE_LINKS]:
                label = classify(u)[1] if classify(u) else "link"
                push(link_line(u, label))
        if t.social_links:
            push(f"<b>🌐 Social links ({len(t.social_links)}):</b>")
            for u in t.social_links[:MAX_SOCIAL_LINKS]:
                push(link_line(u, "profile"))
        if t.image_links:
            push(f"<b>🖼 Image hosts ({len(t.image_links)}):</b>")
            for u in t.image_links[:MAX_IMAGE_LINKS]:
                push(link_line(u, "gallery"))

    if errors:
        push("")
        push("<b>⚠ Errors:</b>")
        for e in errors:
            push(f"• {html_esc.escape(e)}")

    if cur:
        parts.append("\n".join(cur))
    return parts


# ---------------------------------------------------------------------------
# The blocking search job (runs in a worker thread)
# ---------------------------------------------------------------------------


def run_search(query: str, status: Callable[[str], None]) -> List[ThreadInfo]:
    sc = ForumSession(FORUM_USERNAME, FORUM_PASSWORD)
    status("logged in, searching forum...")
    hits = search_forum(sc, query)
    if not hits:
        return []

    status(f"{len(hits)} hit(s) found — crawling threads...")
    threads: List[ThreadInfo] = []
    errors: List[str] = []
    for i, hit in enumerate(hits[:MAX_THREADS_TO_CRAWL], 1):
        try:
            status(f"crawling thread {i}/{min(len(hits), MAX_THREADS_TO_CRAWL)}...")
            info = crawl_thread(sc, hit.url)
            info.title = hit.title or info.title
            threads.append(info)
        except Exception as e:  # noqa: BLE001
            log.warning("Crawl failed for %s: %s", hit.url, e)
            errors.append(f"{hit.title[:50]}: {e}")

    if errors:
        # attach errors to the last thread for the report builders
        threads and setattr(threads[-1], "_errors", errors)
    return threads


# ---------------------------------------------------------------------------
# Telegram bot handlers
# ---------------------------------------------------------------------------

BUSY_CHATS: Set[int] = set()


def _allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


async def _handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    chat = update.effective_chat
    msg = update.effective_message

    if not _allowed(chat.id):
        await msg.reply_text("⛔ This chat is not allowed to use this bot.")
        return
    if chat.id in BUSY_CHATS:
        await msg.reply_text("⏳ A search is already running here — please wait.")
        return
    if len(query) < 2 or len(query) > 80:
        await msg.reply_text("Please send a name between 2 and 80 characters.")
        return

    BUSY_CHATS.add(chat.id)
    status_msg = await msg.reply_text(f"🔎 Searching the forum for “{query}” ...")
    loop_status = {"text": ""}

    def status(text: str) -> None:
        if text != loop_status["text"]:
            loop_status["text"] = text
            log.info("Status: %s", text)

    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
        threads = await asyncio.wait_for(
            asyncio.to_thread(run_search, query, status), timeout=180
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text("⌛ Search took too long (over 3 min) — try a more specific name.")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Search failed")
        await status_msg.edit_text(f"❌ Search failed: {html_esc.escape(str(e)[:300])}")
        return
    finally:
        BUSY_CHATS.discard(chat.id)

    if not threads:
        await status_msg.edit_text(f"😴 Nothing found on the forum for “{query}”.")
        return

    errors = getattr(threads[-1], "_errors", [])
    plain = build_report(query, threads, errors)

    if len(plain) > 12000:
        # Too big for messages — send as a text file.
        buf = io.BytesIO(plain.encode("utf-8"))
        buf.name = f"forum_re.sub(r'[^a-z0-9]+', '_', query.lower()).strip('_') or 'report'}.txt"
        await context.bot.send_document(
            chat_id=chat.id,
            document=buf,
            caption=f"📋 Full report for “{query}” — {len(threads)} thread(s), "
            f"{sum(len(t.file_links) for t in threads)} content links.",
        )
        await status_msg.edit_text(f"✅ Done — report attached above.")
    else:
        await status_msg.delete()
        for chunk in build_html_chunks(query, threads, errors):
            await context.bot.send_message(
                chat_id=chat.id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 <b>Forum Finder</b>\n"
        "Send me a person's name and I'll search the board, crawl their "
        "thread(s) and reply with every content/social link I can find.\n\n"
        "Commands:\n"
        "/find &lt;name&gt; — search\n"
        "/id — show this chat's id (for ALLOWED_CHAT_IDS)",
        parse_mode=ParseMode.HTML,
    )


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args or []).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /find <name>")
        return
    await _handle_search(update, context, query)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"chat id: <code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Plain text (non-command) also triggers a search — easy usage.
    query = (update.effective_message.text or "").strip()
    if re.match(r"^https?://", query):  # ignore people pasting links
        return
    await _handle_search(update, context, query)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot starting (forum: %s, allowed chats: %s)", DOMAIN, ALLOWED_CHAT_IDS or "all")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
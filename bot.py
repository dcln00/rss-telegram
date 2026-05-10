import asyncio
import json
import logging
import re
import signal
import sys
from calendar import timegm
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import feedparser
from dotenv import load_dotenv
from os import getenv
from telegram import Bot

load_dotenv()

TELEGRAM_BOT_TOKEN = getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = getenv("TELEGRAM_CHAT_ID")
FEEDS_FILE = Path("feeds.txt")
SEEN_FILE = Path("seen.json")
BLACKLIST_FILE = Path("blacklist.txt")
CHECK_INTERVAL_SECONDS = 1800  # 30 minutes
MAX_MESSAGE_LENGTH = 4096
USER_AGENT = "rss-telegram-bot/1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_blacklist() -> list[str]:
    if not BLACKLIST_FILE.exists():
        return []
    lines = BLACKLIST_FILE.read_text().splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.strip().startswith("#")]


def load_feeds() -> list[str]:
    if not FEEDS_FILE.exists():
        log.error("feeds.txt not found")
        return []
    lines = FEEDS_FILE.read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text())
        return set(data)
    except (json.JSONDecodeError, TypeError):
        log.warning("Corrupted seen.json, starting fresh")
        return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(list(seen), indent=2))


def get_entry_id(entry: feedparser.FeedParserDict) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def fetch_feed(url: str) -> tuple[str, list[feedparser.FeedParserDict]]:
    feed = feedparser.parse(url, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        log.warning("Feed error for %s: %s", url, feed.bozo_exception)
    title = feed.feed.get("title", url)
    return title, feed.entries


EXCERPT_MAX_LENGTH = 200


def strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", "", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def get_excerpt(entry: feedparser.FeedParserDict) -> str:
    raw = entry.get("summary") or entry.get("description") or ""
    text = strip_html(raw)
    if len(text) > EXCERPT_MAX_LENGTH:
        return text[:EXCERPT_MAX_LENGTH].rsplit(" ", 1)[0] + "..."
    return text


def get_published_time(entry: feedparser.FeedParserDict) -> str:
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not time_struct:
        return ""
    dt = datetime.fromtimestamp(timegm(time_struct), tz=timezone.utc)
    return dt.strftime("%b %d, %Y at %H:%M UTC")


def is_published_today(entry: feedparser.FeedParserDict) -> bool:
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not time_struct:
        return False
    dt = datetime.fromtimestamp(timegm(time_struct), tz=timezone.utc)
    return dt.date() == datetime.now(tz=timezone.utc).date()


def format_messages(new_items: dict[str, list[dict]]) -> list[str]:
    messages: list[str] = []
    current = ""

    for feed_title, items in new_items.items():
        block = f"<b>{escape(feed_title)}</b>\n\n"
        for item in items:
            title = escape(item["title"])
            link = item["link"]
            excerpt = escape(item.get("excerpt", ""))
            published = item.get("published", "")

            block += f'<a href="{link}"><b>{title}</b></a>\n'
            if published:
                block += f"<i>{published}</i>\n"
            if excerpt:
                block += f"{excerpt}\n"
            block += "\n"

        if len(current) + len(block) > MAX_MESSAGE_LENGTH:
            if current:
                messages.append(current.strip())
            current = block
        else:
            current += block

    if current.strip():
        messages.append(current.strip())

    return messages


async def send_message(bot: Bot, text: str) -> None:
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def check_feeds(bot: Bot) -> None:
    feeds = load_feeds()
    if not feeds:
        log.warning("No feeds configured in feeds.txt")
        return

    seen = load_seen()
    blacklist = load_blacklist()
    new_items: dict[str, list[dict]] = {}
    newly_seen: set[str] = set()

    for url in feeds:
        try:
            feed_title, entries = fetch_feed(url)
            log.info("Checked: %s (%d entries)", feed_title, len(entries))
        except Exception:
            log.exception("Failed to fetch feed: %s", url)
            continue

        for entry in entries:
            entry_id = get_entry_id(entry)
            if not entry_id or entry_id in seen:
                continue

            has_date = entry.get("published_parsed") or entry.get("updated_parsed")
            if has_date and not is_published_today(entry):
                continue

            title = entry.get("title", "")
            if blacklist and any(word in title.lower() for word in blacklist):
                log.info("Blocked: %s (matched blacklist)", title)
                newly_seen.add(entry_id)
                continue

            if feed_title not in new_items:
                new_items[feed_title] = []

            title = entry.get("title", "Untitled")
            if not has_date:
                title = f"** {title}"

            new_items[feed_title].append({
                "title": title,
                "link": entry.get("link", ""),
                "excerpt": get_excerpt(entry),
                "published": get_published_time(entry),
            })
            newly_seen.add(entry_id)

    if not new_items:
        log.info("No new items found")
        return

    total = sum(len(items) for items in new_items.values())
    log.info("Found %d new items across %d feeds", total, len(new_items))

    messages = format_messages(new_items)
    for msg in messages:
        try:
            await send_message(bot, msg)
        except Exception:
            log.exception("Failed to send message: %s", msg[:100])

    seen.update(newly_seen)
    save_seen(seen)


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set in .env")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID is not set in .env")
        sys.exit(1)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    log.info("Bot started. Checking feeds every %d seconds.", CHECK_INTERVAL_SECONDS)

    while not stop_event.is_set():
        await check_feeds(bot)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

    log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())

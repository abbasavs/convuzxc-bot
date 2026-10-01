"""Twitch -> Telegram автопостинг: анонс, счётчик зрителей, GIF из эфира."""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import (
    FSInputFile,
    InputMediaAnimation,
    LinkPreviewOptions,
    URLInputFile,
)
from dotenv import load_dotenv

load_dotenv()


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise SystemExit(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


# ---------- конфигурация ----------
BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = env("TELEGRAM_CHANNEL_ID")
TWITCH_CLIENT_ID = env("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = env("TWITCH_CLIENT_SECRET")
TWITCH_URL = env("TWITCH_CHANNEL_URL").rstrip("/")
LOGIN = urlparse(TWITCH_URL).path.strip("/").split("/")[0].lower()

POLL_SEC = int(env("POLL_INTERVAL_SEC", "30"))
UPDATE_SEC = int(env("UPDATE_INTERVAL_MIN", "3")) * 60
GIF_DELAY_SEC = int(env("GIF_DELAY_MIN", "15")) * 60
GIF_SECONDS = int(env("GIF_SECONDS", "4"))
OFFLINE_GRACE_SEC = int(env("OFFLINE_GRACE_MIN", "5")) * 60
STREAM_QUALITY = env("STREAM_QUALITY", "480p,360p,worst")
SPACED_DIGITS = env("VIEWERS_SPACED_DIGITS", "false").lower() == "true"
END_ACTION = env("END_ACTION", "edit")  # edit | keep
DB_PATH = env("DB_PATH", "state.db")
# Формат: "Текст|https://url;Текст|https://url"
LINKS_RAW = env("POST_LINKS", f"Twitch|{TWITCH_URL}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")],
)
log = logging.getLogger("twitch-tg")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ---------- состояние (SQLite) ----------
db = sqlite3.connect(DB_PATH)
db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")


def load_state() -> dict | None:
    row = db.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    return json.loads(row[0]) if row else None


def save_state(st: dict | None) -> None:
    if st is None:
        db.execute("DELETE FROM kv WHERE k='state'")
    else:
        db.execute("INSERT OR REPLACE INTO kv VALUES ('state', ?)", (json.dumps(st),))
    db.commit()


# ---------- Twitch ----------
class Twitch:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.token: str | None = None

    async def _auth(self) -> None:
        async with self.s.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        ) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"Twitch auth {r.status}: {body}")
            self.token = json.loads(body)["access_token"]
            log.info("Получен Twitch app-токен")

    async def get_stream(self) -> dict | None:
        """Возвращает данные стрима или None, если канал оффлайн."""
        for attempt in (1, 2):
            if not self.token:
                await self._auth()
            async with self.s.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_login": LOGIN},
                headers={
                    "Client-Id": TWITCH_CLIENT_ID,
                    "Authorization": f"Bearer {self.token}",
                },
            ) as r:
                if r.status == 401 and attempt == 1:
                    self.token = None  # токен протух — перевыпускаем
                    continue
                r.raise_for_status()
                data = (await r.json())["data"]
                return data[0] if data else None
        return None


# ---------- текст поста ----------
def links_block() -> str:
    lines = []
    for part in LINKS_RAW.split(";"):
        if "|" in part:
            title, url = part.split("|", 1)
            lines.append(f'<a href="{escape(url.strip())}">{escape(title.strip())}</a>')
    return "\n".join(lines)


def fmt_viewers(n: int) -> str:
    return " ".join(str(n)) if SPACED_DIGITS else str(n)


def live_text(title: str, game: str, viewers: int) -> str:
    return (
        f"🔴 СТРИИИИМ «{escape(title)}» уже идет!!!\n"
        f"категория: {escape(game or '—')}\n"
        f"👥 {fmt_viewers(viewers)} зрителей на стриме,не хватает только тяя!\n"
        f"кто с тгк,пишите «жопа» в чат\n"
        f"{links_block()}"
    )


def ended_text(title: str, game: str) -> str:
    return (
        f"⚫СТРИИИИМ «{escape(title)}» уже завершен!!!\n"
        f"категория: {escape(game or '—')}\n"
        f"пасиба всем,кто был пакаа\n"
        f"{links_block()}"
    )


# ---------- Telegram ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def tg(fn, *args, retries: int = 4, **kwargs):
    """Вызов Telegram API с повторами при flood-control и сетевых ошибках."""
    for i in range(retries):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            log.warning("Flood control, жду %s c", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except TelegramNetworkError as e:
            log.warning("Сетевая ошибка Telegram (%s), попытка %s", e, i + 1)
            await asyncio.sleep(2**i)
    raise RuntimeError("Telegram API недоступен после повторов")


async def edit_text(st: dict, text: str) -> None:
    if text == st.get("last_text"):
        return
    try:
        if st["has_media"]:
            await tg(bot.edit_message_caption, chat_id=CHANNEL_ID,
                     message_id=st["message_id"], caption=text)
        else:
            await tg(bot.edit_message_text, text=text, chat_id=CHANNEL_ID,
                     message_id=st["message_id"], link_preview_options=NO_PREVIEW)
        st["last_text"] = text
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


def thumb_url(stream: dict) -> str:
    base = stream["thumbnail_url"].replace("{width}", "1280").replace("{height}", "720")
    return f"{base}?t={int(time.time())}"


async def start_stream(stream: dict) -> dict:
    text = live_text(stream["title"], stream["game_name"], stream["viewer_count"])
    # Telegram не умеет превращать текстовое сообщение в медиа через
    # editMessageMedia, поэтому пост стартует с превью стрима (фото),
    # а позже фото заменяется на анимацию.
    try:
        msg = await tg(bot.send_photo, CHANNEL_ID, URLInputFile(thumb_url(stream)), caption=text)
        has_media = True
    except Exception as e:  # превью может быть ещё не готово
        log.warning("Не удалось отправить превью (%s), шлю текстом", e)
        msg = await tg(bot.send_message, CHANNEL_ID, text, link_preview_options=NO_PREVIEW)
        has_media = False
    st = {
        "stream_id": stream["id"],
        "message_id": msg.message_id,
        "has_media": has_media,
        "title": stream["title"],
        "game": stream["game_name"],
        "started_at": datetime.fromisoformat(stream["started_at"].replace("Z", "+00:00")).timestamp(),
        "last_update": time.time(),
        "last_text": text,
        "gif_done": False,
        "gif_attempts": 0,
        "gif_next_try": 0,
        "offline_since": None,
    }
    save_state(st)
    log.info("Стрим %s начался, пост %s создан", stream["id"], msg.message_id)
    return st


async def finish_stream(st: dict) -> None:
    log.info("Стрим %s завершён", st["stream_id"])
    if END_ACTION == "edit":
        try:
            await edit_text(st, ended_text(st["title"], st["game"]))
        except Exception:
            log.exception("Не удалось обновить пост после окончания стрима")


# ---------- GIF ----------
async def run(*cmd: str, timeout: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Таймаут: {cmd[0]}")
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} завершился с кодом {proc.returncode}: "
                           f"{out.decode(errors='ignore')[-300:]}")


async def make_clip() -> Path:
    tmp = Path(tempfile.mkdtemp())
    raw, out = tmp / "raw.ts", tmp / "clip.mp4"
    await run(
        sys.executable, "-m", "streamlink", "--twitch-disable-ads", "-f",
        "--hls-duration", f"00:00:{GIF_SECONDS + 6:02d}",
        "-o", str(raw), TWITCH_URL, STREAM_QUALITY,
        timeout=90,
    )
    await run(
        "ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-t", str(GIF_SECONDS),
        "-an", "-vf", "fps=25,scale=640:-2", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
        timeout=60,
    )
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("Клип не создан")
    return out


async def attach_clip(st: dict, text: str, clip: Path) -> None:
    if st["has_media"]:
        media = InputMediaAnimation(media=FSInputFile(clip), caption=text)
        await tg(bot.edit_message_media, media=media,
                 chat_id=CHANNEL_ID, message_id=st["message_id"])
    else:  # пост был текстовым — пересоздаём с анимацией
        old = st["message_id"]
        msg = await tg(bot.send_animation, CHANNEL_ID, FSInputFile(clip), caption=text)
        st["message_id"], st["has_media"] = msg.message_id, True
        try:
            await bot.delete_message(CHANNEL_ID, old)
        except Exception:
            log.warning("Не удалось удалить старый текстовый пост")
    st["last_text"] = text


# ---------- основной цикл ----------
async def tick(twitch: Twitch) -> None:
    stream = await twitch.get_stream()
    st = load_state()
    now = time.time()

    if stream is None:
        if st:
            if not st["offline_since"]:
                st["offline_since"] = now
                save_state(st)
                log.info("Канал ушёл в оффлайн, жду %s c (возможен рестарт)", OFFLINE_GRACE_SEC)
            elif now - st["offline_since"] >= OFFLINE_GRACE_SEC:
                await finish_stream(st)
                save_state(None)
        return

    if st and st["stream_id"] != stream["id"]:  # стрим перезапущен как новый
        await finish_stream(st)
        save_state(None)
        st = None

    if not st:
        await start_stream(stream)
        return

    if st["offline_since"]:
        log.info("Стрим вернулся до истечения паузы")
        st["offline_since"] = None
    st["title"], st["game"] = stream["title"], stream["game_name"]
    text = live_text(st["title"], st["game"], stream["viewer_count"])

    if now - st["last_update"] >= UPDATE_SEC:
        await edit_text(st, text)
        st["last_update"] = now
        log.info("Зрителей: %s", stream["viewer_count"])

    gif_due = (
        not st["gif_done"]
        and st["gif_attempts"] < 3
        and now - st["started_at"] >= GIF_DELAY_SEC
        and now >= st["gif_next_try"]
    )
    if gif_due:
        try:
            clip = await make_clip()
            await attach_clip(st, text, clip)
            st["gif_done"] = True
            log.info("GIF добавлена к посту")
        except Exception:
            st["gif_attempts"] += 1
            st["gif_next_try"] = now + 60
            log.exception("Ошибка GIF (попытка %s/3)", st["gif_attempts"])
    save_state(st)


async def main() -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        twitch = Twitch(session)
        log.info("Бот запущен, канал: %s", LOGIN)
        try:
            while True:
                try:
                    await tick(twitch)
                except Exception:
                    log.exception("Ошибка в цикле, повтор через %s c", POLL_SEC)
                await asyncio.sleep(POLL_SEC)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено")

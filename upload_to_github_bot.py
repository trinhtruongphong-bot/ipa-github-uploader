#!/usr/bin/env python3
import os
import sys
import asyncio
import threading
import time
from datetime import datetime
from typing import AsyncIterator, Optional

import aiohttp
from aiohttp import web

from aiogram import F
from aiogram import Router
from aiogram import Dispatcher
from aiogram.types import Message
from aiogram.client.bot import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

# ========= ENV & CONSTANTS =========

BOT_TOKEN = os.getenv("BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # e.g. "username/repo"
# Ưu tiên TELEGRAM_API_BASE, fallback BOT_API_BASE để tương thích cũ
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE") or os.getenv("BOT_API_BASE")

RELEASE_TAG = os.getenv("RELEASE_TAG")  # cố định một tag, ví dụ "ipa-files"
RELEASE_TAG_PREFIX = os.getenv("RELEASE_TAG_PREFIX", "build-")  # nếu không set RELEASE_TAG
HEALTH_PORT = int(os.getenv("HEALTH_PORT", os.getenv("PORT", "10000")))
CHUNK_SIZE = 1024 * 1024  # 1 MiB

REQUIRED = [BOT_TOKEN, GITHUB_TOKEN, GITHUB_REPO, TELEGRAM_API_BASE]
if any(v is None or str(v).strip() == "" for v in REQUIRED):
    raise RuntimeError("❌ Thiếu ENV: BOT_TOKEN, GITHUB_TOKEN, GITHUB_REPO, TELEGRAM_API_BASE")

# ========= HEALTH SERVER (no signal handler) =========

def start_health_server():
    async def _run():
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="OK"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
        await site.start()
        print(f"🌐 Health server listening on 0.0.0.0:{HEALTH_PORT}", flush=True)
        while True:
            await asyncio.sleep(3600)
    asyncio.run(_run())

threading.Thread(target=start_health_server, daemon=True).start()

# ========= GITHUB HELPERS =========

def _auth_headers(extra: Optional[dict] = None) -> dict:
    base = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ipa-github-uploader/1.0",
    }
    if extra:
        base.update(extra)
    return base

async def gh_get_release_by_tag(session: aiohttp.ClientSession, tag: str) -> Optional[dict]:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    async with session.get(url, headers=_auth_headers()) as r:
        if r.status == 200:
            return await r.json()
        if r.status == 404:
            return None
        txt = await r.text()
        raise RuntimeError(f"GitHub get release failed [{r.status}]: {txt}")

async def gh_create_release(session: aiohttp.ClientSession, tag: str) -> dict:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
    async with session.post(url, json=payload, headers=_auth_headers()) as r:
        if r.status not in (200, 201):
            txt = await r.text()
            raise RuntimeError(f"GitHub create release failed [{r.status}]: {txt}")
        return await r.json()

async def gh_ensure_release(session: aiohttp.ClientSession, tag: str) -> dict:
    rel = await gh_get_release_by_tag(session, tag)
    if rel is None:
        rel = await gh_create_release(session, tag)
    return rel

async def gh_upload_stream(
    session: aiohttp.ClientSession,
    release: dict,
    filename: str,
    stream: AsyncIterator[bytes],
    content_type: str = "application/octet-stream",
) -> dict:
    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release['id']}/assets"
    params = {"name": filename}
    headers = _auth_headers({"Content-Type": content_type})
    # stream up tới GitHub
    async with session.post(upload_url, params=params, data=stream, headers=headers) as r:
        if r.status not in (200, 201):
            txt = await r.text()
            raise RuntimeError(f"GitHub upload failed [{r.status}]: {txt}")
        return await r.json()

# ========= TELEGRAM FILE STREAM (local Bot API server) =========

async def tg_iter_content(session: aiohttp.ClientSession, url: str) -> AsyncIterator[bytes]:
    async with session.get(url) as resp:
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
            yield chunk

async def tg_file_stream(session: aiohttp.ClientSession, file_id: str) -> AsyncIterator[bytes]:
    """
    Gọi getFile để lấy file_path, sau đó tải từ /file/bot<TOKEN>/<file_path> theo stream.
    """
    # 1) getFile
    get_file_url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/getFile"
    async with session.get(get_file_url, params={"file_id": file_id}) as r:
        r.raise_for_status()
        data = await r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile failed: {data}")
        file_path = data["result"]["file_path"]

    # 2) tải file theo stream
    file_url = f"{TELEGRAM_API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
    async for part in tg_iter_content(session, file_url):
        yield part

# ========= BOT HANDLERS =========

router = Router()

@router.message(F.document)
async def on_document(msg: Message, bot: Bot):
    doc = msg.document
    if not doc:
        return

    filename = doc.file_name or "file.bin"
    if not filename.lower().endswith(".ipa"):
        await msg.reply("❗ Vui lòng gửi file .ipa")
        return

    await msg.reply("⏳ Đang chuẩn bị tải lên GitHub Releases...")

    # Chọn tag
    tag = RELEASE_TAG or (RELEASE_TAG_PREFIX + datetime.utcnow().strftime("%Y%m%d-%H%M%S"))

    # HTTP session chung cho cả TG & GitHub
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=600)
    conn = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=conn) as sess:
        # 1) Bảo đảm release tồn tại
        release = await gh_ensure_release(sess, tag)

        # 2) Chuẩn bị stream từ Telegram
        async def stream() -> AsyncIterator[bytes]:
            # generator từ TG
            async for chunk in tg_file_stream(sess, doc.file_id):
                yield chunk

        # 3) Upload stream sang GitHub (content-type tùy ý)
        try:
            asset = await gh_upload_stream(sess, release, filename, stream())
        except Exception as e:
            await msg.reply(f"❌ Upload thất bại: {e}")
            raise

    html_url = asset.get("browser_download_url") or asset.get("url")
    await msg.reply(f"✅ Tải lên thành công!\nTag: <code>{tag}</code>\nFile: <b>{filename}</b>\n👉 {html_url}", parse_mode="HTML")

@router.message(F.text)
async def on_text(msg: Message):
    await msg.reply("👋 Gửi mình một file .ipa và mình sẽ upload vào GitHub Releases cho bạn nhé!")

# ========= MAIN =========

async def main():
    # Dùng Bot API server riêng
    api = TelegramAPIServer.from_base(TELEGRAM_API_BASE.rstrip("/"))
    session = AiohttpSession(api=api)
    bot = Bot(token=BOT_TOKEN, session=session)

    # Xóa webhook để tránh 409 Conflict khi polling
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        # Nếu không có webhook cũng không sao
        pass

    dp = Dispatcher()
    dp.include_router(router)
    print("🤖 Starting bot polling...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

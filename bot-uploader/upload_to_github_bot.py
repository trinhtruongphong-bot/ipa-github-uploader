# -*- coding: utf-8 -*-
"""
Telegram IPA → GitHub Releases Uploader (Render Web Service)
- Health server (aiohttp) chạy cùng event loop (KHÔNG dùng thread)
- Bot aiogram v3 (polling), trỏ tới self-hosted telegram-bot-api
- Upload asset lên GitHub Releases, tự tạo release theo tag ngày
"""

import os
import asyncio
import time
from dataclasses import dataclass

from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.client.telegram import TelegramAPIServer

# ========= ENV =========
BOT_TOKEN     = os.environ["BOT_TOKEN"]            # token từ @BotFather
BOT_API_BASE  = os.environ["BOT_API_BASE"]         # ví dụ: https://telegram-bot-api-server-xxx.onrender.com
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]         # PAT (scope repo)
GITHUB_REPO   = os.environ["GITHUB_REPO"]          # ví dụ: trinhtruongphong-bot/ipa-storage
PORT          = int(os.environ.get("PORT", "8080"))  # Render cung cấp biến PORT
RELEASE_PREFIX = os.environ.get("RELEASE_TAG_PREFIX", "uploads")  # prefix tag (mặc định "uploads")

# ========= AIROGRAM (custom server) =========
server = TelegramAPIServer.from_base(BOT_API_BASE)
bot = Bot(token=BOT_TOKEN, server=server)
dp = Dispatcher()

# ========= HEALTH SERVER (async, no thread) =========
async def _health(_):
    return web.Response(text="ok")

async def run_health_server_async():
    app = web.Application()
    app.add_routes([web.get("/", _health), web.get("/health", _health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"🌐 Health server listening on 0.0.0.0:{PORT}", flush=True)
    await asyncio.Event().wait()  # giữ sống

# ========= GITHUB HELPERS =========
@dataclass
class ReleaseInfo:
    id: int
    upload_url: str   # ...{?name,label}
    html_url: str

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _release_tag_today() -> str:
    # mỗi ngày một tag: uploads-YYYYMMDD
    return f"{RELEASE_PREFIX}-{time.strftime('%Y%m%d', time.gmtime())}"

async def gh_ensure_release(session: ClientSession, tag: str) -> ReleaseInfo:
    owner, repo = GITHUB_REPO.split("/", 1)
    base = f"https://api.github.com/repos/{owner}/{repo}"

    # check release by tag
    async with session.get(f"{base}/releases/tags/{tag}", headers=_gh_headers()) as r:
        if r.status == 200:
            data = await r.json()
            return ReleaseInfo(id=data["id"], upload_url=data["upload_url"], html_url=data["html_url"])

    # create if not exists
    payload = {
        "tag_name": tag,
        "name": f"{tag}",
        "body": f"Automated uploads for tag {tag}",
        "draft": False,
        "prerelease": False,
    }
    async with session.post(f"{base}/releases", json=payload, headers=_gh_headers()) as r:
        r.raise_for_status()
        data = await r.json()
        return ReleaseInfo(id=data["id"], upload_url=data["upload_url"], html_url=data["html_url"])

async def gh_upload_asset_stream(session: ClientSession, release: ReleaseInfo, filename: str, stream) -> str:
    """
    Upload stream (async generator or bytes) to GitHub uploads endpoint.
    Tự xử lý trùng tên (422) bằng cách đổi tên file kèm timestamp.
    """
    upload_base = release.upload_url.split("{", 1)[0]
    params = {"name": filename}
    headers = _gh_headers()
    headers["Content-Type"] = "application/octet-stream"

    async with session.post(upload_base, params=params, data=stream, headers=headers) as r:
        if r.status == 422:
            # conflict name -> đổi tên
            params["name"] = f"{int(time.time())}_{filename}"
            async with session.post(upload_base, params=params, data=stream, headers=headers) as r2:
                r2.raise_for_status()
                j2 = await r2.json()
                return j2["browser_download_url"]
        r.raise_for_status()
        j = await r.json()
        return j["browser_download_url"]

# ========= TELEGRAM FILE DOWNLOAD (stream) =========
async def telegram_download_stream(file_path: str):
    """
    Trả về async generator để stream từ Telegram Bot API (self-hosted).
    """
    url = f"{BOT_API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
    async with ClientSession() as s:
        async with s.get(url) as r:
            r.raise_for_status()
            async for chunk in r.content.iter_chunked(1024 * 1024):  # 1MB/chunk
                yield chunk

def is_supported(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith(".ipa") or n.endswith(".plist") or n.endswith(".zip")

# ========= HANDLERS =========
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Chào bạn 👋\n"
        "• Gửi file `.ipa` (hoặc `.plist`/`.zip`), mình sẽ upload lên **GitHub Releases** và trả link tải.\n"
        "• Kích thước tối đa phụ thuộc GitHub Releases (tối đa ~2GB/asset)."
    )

@dp.message(F.document)
async def on_doc(m: Message):
    doc = m.document
    filename = doc.file_name or "file.bin"
    if not is_supported(filename):
        await m.reply("❌ Chỉ hỗ trợ: `.ipa`, `.plist`, `.zip`.")
        return

    status = await m.reply("⏳ Đang lấy file từ Telegram...")

    # 1) get file path
    tg_file = await bot.get_file(doc.file_id)
    # 2) create stream from Telegram
    stream = telegram_download_stream(tg_file.file_path)

    # 3) upload to GitHub
    await status.edit_text("⬆️ Đang upload lên GitHub Releases...")
    async with ClientSession() as session:
        tag = _release_tag_today()
        rel = await gh_ensure_release(session, tag)
        dl_url = await gh_upload_asset_stream(session, rel, filename, stream)

    await status.edit_text(
        "✅ Xong!\n"
        f"• File: `{filename}`\n"
        f"• Link tải: {dl_url}\n"
        f"• Release page: {rel.html_url}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

# ========= MAIN =========
async def start_bot():
    print("🤖 Starting bot polling...", flush=True)
    await dp.start_polling(bot, allowed_updates=["message"])

async def main():
    # Chạy health server & bot song song trong cùng event loop
    await asyncio.gather(
        run_health_server_async(),
        start_bot(),
    )

if __name__ == "__main__":
    asyncio.run(main())

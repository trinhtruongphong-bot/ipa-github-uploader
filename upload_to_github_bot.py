#!/usr/bin/env python3
import os, sys, asyncio, threading
from datetime import datetime
from typing import AsyncIterator, Optional
import aiohttp
from aiogram import F, Router, Dispatcher
from aiogram.types import Message
from aiogram.client.bot import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")               # ví dụ: "trinhtruongphong-bot/ipa-storage"
TELEGRAM_API_BASE = (os.getenv("TELEGRAM_API_BASE") or os.getenv("BOT_API_BASE") or "").rstrip("/")
RELEASE_TAG = os.getenv("RELEASE_TAG")
RELEASE_TAG_PREFIX = os.getenv("RELEASE_TAG_PREFIX", "build-")
HEALTH_PORT = int(os.getenv("PORT", os.getenv("HEALTH_PORT", "10000")))
CHUNK_SIZE = 1024 * 1024

for k, v in {
    "BOT_TOKEN": BOT_TOKEN, "GITHUB_TOKEN": GITHUB_TOKEN,
    "GITHUB_REPO": GITHUB_REPO, "TELEGRAM_API_BASE": TELEGRAM_API_BASE
}.items():
    if not v:
        raise RuntimeError("❌ Thiếu ENV: BOT_TOKEN, GITHUB_TOKEN, GITHUB_REPO, TELEGRAM_API_BASE")

# ====== HEALTH SERVER (http.server – không đụng asyncio/signal) ======
def start_health_server():
    import http.server, socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Trả 200 cho mọi path, Render chỉ cần process lắng trên $PORT
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, fmt, *args):
            # giảm rác log
            return

    # “reuse_port” để tránh lỗi khi restart nhanh
    class TCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    with TCPServer(("0.0.0.0", HEALTH_PORT), Handler) as httpd:
        print(f"🌐 Health server RUNNING on 0.0.0.0:{HEALTH_PORT}", flush=True)
        httpd.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# ====== GitHub helpers ======
def _auth_headers(extra: Optional[dict] = None) -> dict:
    base = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ipa-github-uploader/1.0",
    }
    if extra: base.update(extra)
    return base

async def gh_get_release_by_tag(session, tag):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    async with session.get(url, headers=_auth_headers()) as r:
        if r.status == 200: return await r.json()
        if r.status == 404: return None
        raise RuntimeError(f"GitHub get release failed [{r.status}]: {await r.text()}")

async def gh_create_release(session, tag):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
    async with session.post(url, json=payload, headers=_auth_headers()) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"GitHub create release failed [{r.status}]: {await r.text()}")
        return await r.json()

async def gh_ensure_release(session, tag):
    return await gh_get_release_by_tag(session, tag) or await gh_create_release(session, tag)

async def gh_upload_stream(session, release, filename, stream, content_type="application/octet-stream"):
    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release['id']}/assets"
    params = {"name": filename}
    headers = _auth_headers({"Content-Type": content_type})
    async with session.post(upload_url, params=params, data=stream, headers=headers) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"GitHub upload failed [{r.status}]: {await r.text()}")
        return await r.json()

# ====== Telegram file stream (Bot API riêng) ======
async def tg_iter_content(session, url):
    async with session.get(url) as resp:
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
            yield chunk

async def tg_file_stream(session, file_id: str):
    get_file_url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/getFile"
    async with session.get(get_file_url, params={"file_id": file_id}) as r:
        r.raise_for_status()
        data = await r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile failed: {data}")
        file_path = data["result"]["file_path"]
    file_url = f"{TELEGRAM_API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
    async for part in tg_iter_content(session, file_url):
        yield part

# ====== Bot handlers ======
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

    await msg.reply("⏳ Đang tải lên GitHub Releases…")

    tag = RELEASE_TAG or (RELEASE_TAG_PREFIX + datetime.utcnow().strftime("%Y%m%d-%H%M%S"))
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=600)
    conn = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)

    async with aiohttp.ClientSession(timeout=timeout, connector=conn) as sess:
        release = await gh_ensure_release(sess, tag)

        async def stream():
            async for chunk in tg_file_stream(sess, doc.file_id):
                yield chunk

        try:
            asset = await gh_upload_stream(sess, release, filename, stream())
        except Exception as e:
            await msg.reply(f"❌ Upload thất bại: {e}")
            raise

    url = asset.get("browser_download_url") or asset.get("url")
    await msg.reply(
        f"✅ Thành công!\nTag: <code>{tag}</code>\nFile: <b>{filename}</b>\n👉 {url}",
        parse_mode="HTML",
    )

@router.message(F.text)
async def on_text(msg: Message):
    await msg.reply("👋 Gửi file .ipa, mình sẽ upload lên GitHub Releases cho bạn nhé!")

# ====== Main ======
async def main():
    api = TelegramAPIServer.from_base(TELEGRAM_API_BASE)
    session = AiohttpSession(api=api)
    bot = Bot(token=BOT_TOKEN, session=session)

    # tránh 409 (xung đột polling)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    dp = Dispatcher()
    dp.include_router(router)
    print("🤖 Bot đang polling...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

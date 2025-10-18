import os
import asyncio
import aiohttp
from aiohttp import ClientTimeout
from urllib.parse import quote
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.filters import Command
from contextlib import asynccontextmanager
import tempfile

BOT_TOKEN = os.getenv("BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # e.g. "trinhtruongphong-bot/ipa-storage"
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE")  # e.g. "https://telegram-bot-api-server-xxxx.onrender.com"

if not (BOT_TOKEN and GITHUB_TOKEN and GITHUB_REPO and TELEGRAM_API_BASE):
    raise RuntimeError("❌ Thiếu ENV: BOT_TOKEN, GITHUB_TOKEN, GITHUB_REPO, TELEGRAM_API_BASE")

# Chuẩn hoá BASE (không có dấu / ở cuối)
TELEGRAM_API_BASE = TELEGRAM_API_BASE.rstrip("/")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ===== Helpers =====
@asynccontextmanager
async def http():
    timeout = ClientTimeout(total=None, sock_connect=30, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        yield s

async def gh_ensure_release(session: aiohttp.ClientSession, tag: str):
    # Lấy release theo tag (có -> dùng; không có -> tạo)
    base = f"https://api.github.com/repos/{GITHUB_REPO}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    # List releases và tìm tag
    async with session.get(f"{base}/releases", headers=headers) as r:
        r.raise_for_status()
        releases = await r.json()
    for rel in releases:
        if rel.get("tag_name") == tag:
            return rel
    # Tạo mới
    payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
    async with session.post(f"{base}/releases", json=payload, headers=headers) as r:
        r.raise_for_status()
        return await r.json()

async def tg_download_to_file(session: aiohttp.ClientSession, file_id: str, progress_cb=None):
    """
    Tải file Telegram về file tạm, trả về (path, size, original_filename)
    Dùng Bot API server tự host: /bot<token>/getFile + /file/bot<token>/<file_path>
    """
    # 1) getFile
    get_file_url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/getFile"
    async with session.post(get_file_url, json={"file_id": file_id}) as r:
        r.raise_for_status()
        data = await r.json()
    result = data.get("result") or {}
    file_path = result.get("file_path")
    if not file_path:
        raise RuntimeError("Không lấy được file_path từ Telegram.")

    # Suy ra tên gốc (nếu có)
    original_name = os.path.basename(file_path)

    # 2) tải binary
    file_url = f"{TELEGRAM_API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
    chunk = 1024 * 1024  # 1MB
    total = 0
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    try:
        async with session.get(file_url) as r:
            r.raise_for_status()
            async for part in r.content.iter_chunked(chunk):
                total += len(part)
                tmp.write(part)
                if progress_cb and total % (5 * 1024 * 1024) < chunk:  # báo mỗi ~5MB
                    await progress_cb(total)
        tmp.flush()
        tmp.close()
        return tmp_path, total, original_name
    except Exception:
        try:
            tmp.close()
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise

async def gh_upload_file(session: aiohttp.ClientSession, release: dict, filename: str, file_path: str, size: int):
    """
    Upload file tạm lên GitHub Releases (bắt buộc Content-Length)
    """
    upload_base = release["upload_url"].split("{", 1)[0]
    params = {"name": filename}
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/octet-stream",
        "Accept": "application/vnd.github+json",
        "Content-Length": str(size),
    }
    # Mở file truyền thẳng (không load vào RAM)
    async with session.post(f"{upload_base}?name={quote(filename)}", data=open(file_path, "rb"), headers=headers) as r:
        # GitHub trả 201 nếu ok
        if r.status not in (200, 201):
            txt = await r.text()
            raise RuntimeError(f"Upload thất bại ({r.status}): {txt}")
        return await r.json()

# ===== Handlers =====
@dp.message(Command("start"))
async def on_start(m: Message):
    await m.answer("Gửi mình file .ipa, mình sẽ upload lên GitHub Release và trả link.\n"
                   "• Hỗ trợ file lớn > 50MB\n"
                   "• Server Telegram: dùng Bot API riêng")

@dp.message(F.document)
async def on_doc(m: Message):
    doc = m.document
    filename = doc.file_name or "app.ipa"
    status = await m.answer(f"📥 Nhận file **{filename}**. Đang xử lý...", parse_mode=ParseMode.MARKDOWN)

    try:
        async with http() as session:
            # Tải về file tạm + báo tiến độ
            last_mb = 0
            async def progress(bytes_so_far):
                nonlocal last_mb
                mb = bytes_so_far // (1024 * 1024)
                if mb >= last_mb + 5:
                    last_mb = mb
                    try:
                        await status.edit_text(f"⬇️ Đang tải từ Telegram: {mb} MB...")
                    except Exception:
                        pass

            tmp_path, size, _ = await tg_download_to_file(session, doc.file_id, progress_cb=progress)
            await status.edit_text(f"✅ Tải xong từ Telegram ({size//(1024*1024)} MB). Đang tạo Release...")

            # Tạo/tìm release theo ngày hoặc tag cố định
            tag = "uploads"
            release = await gh_ensure_release(session, tag)

            await status.edit_text("⬆️ Đang upload lên GitHub Releases...")
            asset = await gh_upload_file(session, release, filename, tmp_path, size)

            url = asset.get("browser_download_url")
            await status.edit_text(f"✅ Xong! Tải tại:\n{url}")
    except Exception as e:
        try:
            await status.edit_text(f"❌ Lỗi: {e}")
        except Exception:
            pass

def main():
    # Health server đơn giản (không dùng signal ở thread phụ để tránh lỗi)
    import threading
    from aiohttp import web

    async def health(_):
        return web.Response(text="ok")

    def run_health():
        app = web.Application()
        app.add_routes([web.get("/", health)])
        web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

    threading.Thread(target=run_health, daemon=True).start()

    dp.run_polling(bot, allowed_updates=["message", "edited_message"])

if __name__ == "__main__":
    main()

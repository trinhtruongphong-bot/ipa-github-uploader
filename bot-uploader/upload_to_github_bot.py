import os
import json
import asyncio
from typing import AsyncIterator, Optional

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ContentType

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # ví dụ: "trinhtruongphong-bot/ipa-storage"
RELEASE_TAG_PREFIX = os.getenv("RELEASE_TAG_PREFIX", "build-")  # tag mặc định
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "")  # ví dụ: "https://<your-local-bot-api>.onrender.com"
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "10000"))

if not (BOT_TOKEN and GITHUB_TOKEN and GITHUB_REPO and TELEGRAM_API_BASE):
    raise RuntimeError("Thiếu biến môi trường: BOT_TOKEN / GITHUB_TOKEN / GITHUB_REPO / TELEGRAM_API_BASE")

# ====== Aiogram setup (dùng Local Bot API) ======
bot = Bot(token=BOT_TOKEN, api=TELEGRAM_API_BASE.rstrip("/"))
dp = Dispatcher()


# ====== GitHub helpers ======
async def gh_headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def gh_ensure_release(session: ClientSession, tag: str) -> dict:
    """Tạo (hoặc lấy) release theo tag."""
    # Thử GET release theo tag
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    async with session.get(url, headers=await gh_headers()) as r:
        if r.status == 200:
            return await r.json()
    # Không có -> tạo mới
    create_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
    async with session.post(create_url, headers=await gh_headers(), json=payload) as r:
        r.raise_for_status()
        return await r.json()


async def gh_upload_stream(
    session: ClientSession,
    release: dict,
    filename: str,
    stream: AsyncIterator[bytes],
) -> dict:
    """Upload file stream lên assets của release."""
    upload_url_template = release["upload_url"]  # dạng ...{?name,label}
    upload_base = upload_url_template.split("{")[0]
    params = {"name": filename}
    headers = {
        **(await gh_headers()),
        "Content-Type": "application/octet-stream",
    }
    # POST streaming body đến uploads.github.com
    async with session.post(upload_base, params=params, data=stream, headers=headers) as r:
        # Nếu asset đã tồn tại -> xóa rồi up lại
        if r.status == 422:
            # get assets -> delete trùng tên
            assets_url = release["assets_url"]
            async with session.get(assets_url, headers=await gh_headers()) as rr:
                rr.raise_for_status()
                assets = await rr.json()
            for a in assets:
                if a.get("name") == filename:
                    del_url = a["url"]
                    async with session.delete(del_url, headers=await gh_headers()) as d:
                        d.raise_for_status()
                    break
            # upload lại
            async with session.post(upload_base, params=params, data=stream, headers=headers) as r2:
                r2.raise_for_status()
                return await r2.json()
        r.raise_for_status()
        return await r.json()


# ====== Telegram file streaming ======
async def tg_file_stream(session: ClientSession, file_id: str) -> AsyncIterator[bytes]:
    """Lấy file_path từ getFile rồi stream nội dung từ Local Bot API /file/..."""
    # 1) getFile để lấy file_path
    gf_url = f"{TELEGRAM_API_BASE.rstrip('/')}/bot{BOT_TOKEN}/getFile"
    async with session.post(gf_url, json={"file_id": file_id}) as r:
        r.raise_for_status()
        data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getFile failed: {data}")

    file_path = data["result"]["file_path"]
    # 2) stream từ endpoint /file/botTOKEN/<file_path>
    file_url = f"{TELEGRAM_API_BASE.rstrip('/')}/file/bot{BOT_TOKEN}/{file_path}"

    timeout = ClientTimeout(total=None, sock_connect=60, sock_read=None)
    async with session.get(file_url, timeout=timeout) as resp:
        resp.raise_for_status()
        async for chunk in resp.content.iter_chunked(1024 * 256):  # 256KB/chunk
            yield chunk


# ====== Bot handlers ======
@dp.message(F.content_type == ContentType.DOCUMENT)
async def on_doc(message: Message):
    doc = message.document
    filename = doc.file_name or f"file-{doc.file_unique_id}"
    tag = f"{RELEASE_TAG_PREFIX}{message.date:%Y%m%d}"

    await message.answer(f"⏳ Đang tải **{filename}** lên GitHub Release `{tag}` …")

    timeout = ClientTimeout(total=None)
    async with ClientSession(timeout=timeout) as session:
        # 1) create/fetch release
        release = await gh_ensure_release(session, tag)

        # 2) stream từ Telegram -> GitHub uploads
        file_stream = tg_file_stream(session, doc.file_id)
        asset = await gh_upload_stream(session, release, filename, file_stream)

        html_url: Optional[str] = asset.get("browser_download_url")
        size = asset.get("size")
        msg = f"✅ Xong: **{filename}** ({size} bytes)"
        if html_url:
            msg += f"\n🔗 {html_url}"
        await message.answer(msg)


@dp.message()
async def fallback(message: Message):
    await message.answer("Gửi file .ipa (hoặc bất kỳ file nào) cho mình để up lên GitHub Release nhé.")


# ====== health server (chạy cùng loop, không dùng thread để tránh set_wakeup_fd error) ======
async def start_health_app():
    app = web.Application()
    async def ping(_):
        return web.json_response({"status": "ok"})
    app.router.add_get("/", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()


async def main():
    await start_health_app()
    print(f"🌐 Health server listening on 0.0.0.0:{HEALTH_PORT}")
    print("🤖 Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

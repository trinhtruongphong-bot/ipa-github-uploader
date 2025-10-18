import os, asyncio, time
from dataclasses import dataclass
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.client.telegram import TelegramAPIServer

# ===== ENV =====
BOT_TOKEN    = os.environ["BOT_TOKEN"]
BOT_API_BASE = os.environ["BOT_API_BASE"]          # ví dụ: https://telegram-bot-api-server-jsy3.onrender.com
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO  = os.environ["GITHUB_REPO"]           # ví dụ: trinhtruongphong-bot/ipa-storage
PORT         = int(os.environ.get("PORT", "8080"))
TAG_PREFIX   = os.environ.get("RELEASE_TAG_PREFIX", "uploads")

# ===== AIROGRAM =====
server = TelegramAPIServer.from_base(BOT_API_BASE)
bot = Bot(token=BOT_TOKEN, server=server)
dp = Dispatcher()

# ===== Health server (async – no thread) =====
async def _health(_):
    return web.Response(text="ok")

async def run_health_server_async():
    app = web.Application()
    app.add_routes([web.get("/", _health), web.get("/health", _health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Health server listening on 0.0.0.0:{PORT}", flush=True)
    await asyncio.Event().wait()

# ===== GitHub helpers =====
@dataclass
class ReleaseInfo:
    id: int
    upload_url: str
    html_url: str

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _tag_today():
    return f"{TAG_PREFIX}-{time.strftime('%Y%m%d', time.gmtime())}"

async def gh_ensure_release(session: ClientSession, tag: str) -> ReleaseInfo:
    owner, repo = GITHUB_REPO.split("/", 1)
    base = f"https://api.github.com/repos/{owner}/{repo}"

    async with session.get(f"{base}/releases/tags/{tag}", headers=_gh_headers()) as r:
        if r.status == 200:
            j = await r.json()
            return ReleaseInfo(j["id"], j["upload_url"], j["html_url"])

    payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
    async with session.post(f"{base}/releases", json=payload, headers=_gh_headers()) as r:
        r.raise_for_status()
        j = await r.json()
        return ReleaseInfo(j["id"], j["upload_url"], j["html_url"])

async def gh_upload_stream(session: ClientSession, rel: ReleaseInfo, filename: str, stream):
    upload_base = rel.upload_url.split("{", 1)[0]
    params = {"name": filename}
    headers = _gh_headers()
    headers["Content-Type"] = "application/octet-stream"

    async with session.post(upload_base, params=params, data=stream, headers=headers) as r:
        if r.status == 422:  # trùng tên
            params["name"] = f"{int(time.time())}_{filename}"
            async with session.post(upload_base, params=params, data=stream, headers=headers) as r2:
                r2.raise_for_status()
                j2 = await r2.json()
                return j2["browser_download_url"]
        r.raise_for_status()
        j = await r.json()
        return j["browser_download_url"]

# ===== Telegram file stream =====
async def tg_stream(file_path: str):
    url = f"{BOT_API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
    async with ClientSession() as s:
        async with s.get(url) as r:
            r.raise_for_status()
            async for chunk in r.content.iter_chunked(1024 * 1024):
                yield chunk

def _supported(name: str):
    n = (name or "").lower()
    return n.endswith(".ipa") or n.endswith(".plist") or n.endswith(".zip")

# ===== Handlers =====
@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer("Gửi file `.ipa`/`.plist`/`.zip`, mình sẽ upload lên GitHub Releases và trả link tải.")

@dp.message(F.document)
async def on_doc(m: Message):
    doc = m.document
    filename = doc.file_name or "file.bin"
    if not _supported(filename):
        await m.reply("❌ Chỉ hỗ trợ .ipa/.plist/.zip")
        return

    status = await m.reply("⏳ Đang lấy file từ Telegram...")
    tg_file = await bot.get_file(doc.file_id)
    stream = tg_stream(tg_file.file_path)

    await status.edit_text("⬆️ Đang upload lên GitHub...")
    async with ClientSession() as session:
        tag = _tag_today()
        rel = await gh_ensure_release(session, tag)
        dl = await gh_upload_stream(session, rel, filename, stream)

    await status.edit_text(
        f"✅ Xong!\n• File: `{filename}`\n• Link tải: {dl}\n• Release: {rel.html_url}",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

# ===== Main =====
async def start_bot():
    print("🤖 Starting bot polling...", flush=True)
    await dp.start_polling(bot, allowed_updates=["message"])

async def main():
    await asyncio.gather(run_health_server_async(), start_bot())

if __name__ == "__main__":
    asyncio.run(main())

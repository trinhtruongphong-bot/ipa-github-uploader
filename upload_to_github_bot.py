import os
import asyncio
from typing import AsyncIterator, Callable, Awaitable
from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ContentType

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # ví dụ: trinhtruongphong-bot/ipa-storage
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "")  # ví dụ: https://telegram-bot-api-server-xxxxx.onrender.com
RELEASE_TAG_PREFIX = os.getenv("RELEASE_TAG_PREFIX", "build-")
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "10000"))

if not (BOT_TOKEN and GITHUB_TOKEN and GITHUB_REPO and TELEGRAM_API_BASE):
    raise RuntimeError("❌ Thiếu ENV: BOT_TOKEN, GITHUB_TOKEN, GITHUB_REPO, TELEGRAM_API_BASE")

bot = Bot(token=BOT_TOKEN, api=TELEGRAM_API_BASE.rstrip("/"))
dp = Dispatcher()

# ====== Helpers ======
def gh_headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

async def with_retry(fn: Callable[[], Awaitable], tries=5, base_delay=1.5):
    last = None
    for i in range(tries):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 (chủ động catch mọi lỗi network)
            last = e
            await asyncio.sleep(base_delay * (2 ** i))
    raise last

# ====== GitHub ======
async def gh_ensure_release(session: ClientSession, tag: str) -> dict:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    async def _get():
        async with session.get(url, headers=gh_headers()) as r:
            if r.status == 200:
                return await r.json()
            return None
    existing = await with_retry(_get)
    if existing:
        return existing

    create_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
    async def _create():
        async with session.post(create_url, headers=gh_headers(), json=payload) as r:
            r.raise_for_status()
            return await r.json()
    return await with_retry(_create)

async def gh_upload_stream(session: ClientSession, release: dict, filename: str, stream: AsyncIterator[bytes]) -> dict:
    upload_base = release["upload_url"].split("{")[0]
    params = {"name": filename}
    headers = {**gh_headers(), "Content-Type": "application/octet-stream"}

    async def _post():
        async with session.post(upload_base, params=params, data=stream, headers=headers, timeout=ClientTimeout(total=None)) as r:
            # 422 khi asset trùng tên → xóa rồi up lại
            if r.status == 422:
                async with session.get(release["assets_url"], headers=gh_headers()) as rr:
                    rr.raise_for_status()
                    for a in await rr.json():
                        if a.get("name") == filename:
                            async with session.delete(a["url"], headers=gh_headers()) as d:
                                d.raise_for_status()
                            break
                # up lại
                async with session.post(upload_base, params=params, data=stream, headers=headers, timeout=ClientTimeout(total=None)) as r2:
                    r2.raise_for_status()
                    return await r2.json()
            r.raise_for_status()
            return await r.json()

    # retry nếu stream/ mạng đứt quãng
    return await with_retry(_post, tries=4)

# ====== Telegram stream ======
async def tg_file_stream(session: ClientSession, file_id: str) -> AsyncIterator[bytes]:
    # lấy file_path
    gf_url = f"{TELEGRAM_API_BASE.rstrip('/')}/bot{BOT_TOKEN}/getFile"
    data = await with_retry(lambda: session.post(gf_url, json={"file_id": file_id}))
    async with data as r1:
        r1.raise_for_status()
        j = await r1.json()
    if not j.get("ok") or "result" not in j:
        raise RuntimeError(f"getFile thất bại: {j}")
    file_path = j["result"]["file_path"]
    file_url = f"{TELEGRAM_API_BASE.rstrip('/')}/file/bot{BOT_TOKEN}/{file_path}"

    # stream nội dung (không giới hạn 50MB)
    timeout = ClientTimeout(total=None, sock_connect=60, sock_read=None)
    async def _get():
        return await session.get(file_url, timeout=timeout)
    resp = await with_retry(_get)
    async with resp as r2:
        r2.raise_for_status()
        async for chunk in r2.content.iter_chunked(512 * 1024):
            yield chunk

# ====== Handlers ======
@dp.message(F.content_type == ContentType.DOCUMENT)
async def on_doc(message: Message):
    doc = message.document
    filename = doc.file_name or f"file-{doc.file_unique_id}"
    tag = f"{RELEASE_TAG_PREFIX}{message.date:%Y%m%d}"

    await message.answer(f"⏳ Đang upload **{filename}** lên GitHub Release `{tag}`...")

    connector = TCPConnector(limit=0, ttl_dns_cache=300)
    async with ClientSession(connector=connector, timeout=ClientTimeout(total=None), trust_env=True) as session:
        release = await gh_ensure_release(session, tag)
        stream = tg_file_stream(session, doc.file_id)   # AsyncIterator[bytes]
        asset = await gh_upload_stream(session, release, filename, stream)

    await message.answer(
        "✅ Hoàn tất!\n"
        f"• Tên: **{filename}**\n"
        f"• Link: {asset.get('browser_download_url')}"
    )

@dp.message()
async def help_msg(message: Message):
    await message.answer("📦 Hãy gửi file .ipa (hoặc bất kỳ file nào). Mình sẽ upload trực tiếp vào GitHub Release.")

# ====== Health ======
async def start_health():
    app = web.Application()
    app.router.add_get("/", lambda _: web.json_response({"status": "ok"}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()

async def main():
    await start_health()
    print(f"🌐 Health server chạy tại 0.0.0.0:{HEALTH_PORT}")
    print("🤖 Bot đang polling…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

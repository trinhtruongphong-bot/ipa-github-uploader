import os
import asyncio
import requests

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.telegram import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession

# --- ENV ---
BOT_TOKEN    = os.environ["BOT_TOKEN"]
BOT_API_BASE = os.environ["BOT_API_BASE"]  # ví dụ: https://telegram-bot-api-server-xxx.onrender.com
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO  = os.environ["GITHUB_REPO"]   # ví dụ: trinhtruongphong-bot/ipa-storage
RELEASE_TAG  = os.getenv("RELEASE_TAG", "ipa-files")

# ---------- GitHub helpers ----------
def gh_headers(extra=None):
    h = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    if extra:
        h.update(extra)
    return h

def ensure_release_and_get_id():
    """Lấy release theo tag; nếu chưa có thì tạo mới, trả về release_id."""
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}",
        headers=gh_headers(),
        timeout=60,
    )
    if r.status_code == 200:
        return r.json()["id"]
    if r.status_code == 404:
        c = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases",
            headers=gh_headers(),
            json={"tag_name": RELEASE_TAG, "name": RELEASE_TAG, "draft": False, "prerelease": False},
            timeout=60,
        )
        c.raise_for_status()
        return c.json()["id"]
    r.raise_for_status()

def upload_to_github(file_path: str, file_name: str) -> str:
    release_id = ensure_release_and_get_id()
    # Nếu trùng tên, GitHub sẽ trả 422; có thể xoá asset cũ nếu muốn (bổ sung sau).
    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
    params = {"name": file_name}
    with open(file_path, "rb") as f:
        resp = requests.post(
            upload_url,
            params=params,
            headers=gh_headers({"Content-Type": "application/octet-stream"}),
            data=f,
            timeout=600,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Upload failed: {resp.status_code} {resp.text[:300]}")
    # Link tải trực tiếp:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{RELEASE_TAG}/{file_name}"

# ---------- Bot ----------
async def main():
    # ✅ Quan trọng: dùng TelegramAPIServer.from_base cho Bot API tự host
    custom_api = TelegramAPIServer.from_base(BOT_API_BASE)
    session = AiohttpSession(api=custom_api)
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()

    @dp.message()
    async def handle_doc(msg: types.Message):
        doc = msg.document
        if not doc:
            await msg.reply("📦 Gửi file `.ipa` mình sẽ upload lên GitHub Releases.", parse_mode=ParseMode.MARKDOWN)
            return
        if not (doc.file_name or "").lower().endswith(".ipa"):
            await msg.reply("❌ Chỉ hỗ trợ file `.ipa`.", parse_mode=ParseMode.MARKDOWN)
            return

        await msg.reply(f"⬆️ Đang tải `{doc.file_name}` lên GitHub…", parse_mode=ParseMode.MARKDOWN)

        # Tải file về tạm (ổ đĩa ephemeral của Render – đủ dùng vì chỉ trung chuyển)
        tmp_path = f"/tmp/{doc.file_name}"
        await bot.download(doc, destination=tmp_path)

        try:
            link = upload_to_github(tmp_path, doc.file_name)
            await msg.reply(f"✅ Upload thành công!\n🔗 [Tải trực tiếp]({link})", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await msg.reply(f"⚠️ Lỗi: `{e}`", parse_mode=ParseMode.MARKDOWN)

    print("🤖 Bot started successfully!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

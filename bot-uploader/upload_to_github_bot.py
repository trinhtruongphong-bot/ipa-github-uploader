import os
import requests
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_API_BASE = os.environ["BOT_API_BASE"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
RELEASE_TAG = os.getenv("RELEASE_TAG", "ipa-files")

# ---------- GitHub Upload Helper ----------
def upload_to_github(file_path: str, file_name: str):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    # Get release ID
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        raise Exception(f"Failed to get release info: {r.text}")
    release_id = r.json()["id"]

    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets?name={file_name}"
    with open(file_path, "rb") as f:
        r = requests.post(upload_url, headers={
            **headers,
            "Content-Type": "application/octet-stream"
        }, data=f)
    if r.status_code not in (200, 201):
        raise Exception(f"Upload failed: {r.text}")

    return f"https://github.com/{GITHUB_REPO}/releases/download/{RELEASE_TAG}/{file_name}"


# ---------- Telegram Bot Logic ----------
async def main():
    # ✅ FIX: use AiohttpSession instead of HTTPXRequest
    session = AiohttpSession(api=BOT_API_BASE)
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher()

    @dp.message()
    async def handle_message(message: types.Message):
        if not message.document:
            await message.reply("📦 Gửi file .ipa để upload lên GitHub Releases.")
            return

        file = message.document
        if not file.file_name.endswith(".ipa"):
            await message.reply("❌ File không hợp lệ. Chỉ hỗ trợ .ipa")
            return

        await message.reply("⬆️ Đang tải lên GitHub...")
        file_path = f"/tmp/{file.file_name}"
        await bot.download(file, file_path)

        try:
            link = upload_to_github(file_path, file.file_name)
            await message.reply(
                f"✅ Tải lên thành công!\n🔗 [Tải về trực tiếp]({link})",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await message.reply(f"⚠️ Lỗi: `{e}`", parse_mode=ParseMode.MARKDOWN)

    print("🤖 Bot started successfully!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

import os, re, urllib.parse, requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.request import HTTPXRequest

BOT_TOKEN     = os.environ["BOT_TOKEN"]
BOT_API_BASE  = os.environ["BOT_API_BASE"]
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GITHUB_REPO   = os.environ["GITHUB_REPO"]
RELEASE_TAG   = os.getenv("RELEASE_TAG", "ipa-files")
GITHUB_API    = "https://api.github.com"

def gh_headers(extra=None):
    h = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    if extra: h.update(extra)
    return h

def ensure_release():
    r = requests.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}", headers=gh_headers())
    if r.status_code == 200:
        return r.json()["id"]
    if r.status_code == 404:
        create = requests.post(f"{GITHUB_API}/repos/{GITHUB_REPO}/releases",
                               headers=gh_headers(),
                               json={"tag_name": RELEASE_TAG, "name": RELEASE_TAG})
        create.raise_for_status()
        return create.json()["id"]
    r.raise_for_status()

def find_asset(release_id, name):
    r = requests.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/{release_id}/assets", headers=gh_headers())
    r.raise_for_status()
    for a in r.json():
        if a["name"] == name:
            return a["id"]
    return None

def delete_asset(asset_id):
    requests.delete(f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/assets/{asset_id}", headers=gh_headers())

def sanitize_filename(name):
    name = re.sub(r"[^\w\.\-]+", "_", name.strip())
    if not name.lower().endswith(".ipa"):
        name += ".ipa"
    return name[:200]

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Gửi file .ipa (tới 2GB). Mình sẽ upload lên GitHub 🚀")

async def handle_ipa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc: return
    fname = sanitize_filename(doc.file_name or "app.ipa")
    size = int(doc.file_size or 0)
    msg = await update.message.reply_text(f"📥 Nhận `{fname}` ({size/1_000_000:.1f} MB)...", parse_mode="Markdown")
    file = await context.bot.get_file(doc.file_id)
    tg_url = f"{BOT_API_BASE}/file/bot{BOT_TOKEN}/{file.file_path}"

    try:
        release_id = ensure_release()
        existing = find_asset(release_id, fname)
        if existing: delete_asset(existing)
        with requests.get(tg_url, stream=True) as rsrc:
            rsrc.raise_for_status()
            rsrc.raw.decode_content = True
            upload_url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release_id}/assets"
            q = urllib.parse.urlencode({"name": fname})
            resp = requests.post(f"{upload_url}?{q}",
                                 headers=gh_headers({"Content-Type": "application/octet-stream",
                                                     "Content-Length": str(size)}),
                                 data=rsrc.raw)
        if resp.status_code == 201:
            dl = resp.json()["browser_download_url"]
            await msg.edit_text(f"✅ Thành công!\n🔗 {dl}", parse_mode="Markdown")
        else:
            await msg.edit_text(f"❌ Lỗi ({resp.status_code})\n{resp.text[:400]}")
    except Exception as e:
        await msg.edit_text(f"❌ Lỗi: `{e}`", parse_mode="Markdown")

def main():
    req = HTTPXRequest(base_url=BOT_API_BASE)
    app = Application.builder().token(BOT_TOKEN).request(req).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_ipa))
    app.run_polling()

if __name__ == "__main__":
    main()

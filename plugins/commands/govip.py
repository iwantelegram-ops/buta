"""
plugins/commands/govip.py
───────────────────────────
Perintah member /govip — promosi "VIP Bio Member" satu grup.

FLOW DI GRUP:
  1. User (member biasa, bukan admin) kirim /govip di grup.
  2. Hapus pesan perintah segera.
  3. Cooldown 5 menit PER GRUP (bukan per user) — siapapun yang memicu
     /govip di grup yang sama akan ditolak diam-diam selama grup itu
     masih dalam masa cooldown. Mencegah spam tombol di grup ramai.
  4. Cek konfigurasi grup:
       a. Jika "Teks VIP Bio" AKTIF (bio_check=True DAN bio_vip_text
          terisi) → balas dengan info sekilas + tombol inline yang
          mengarahkan ke DM bot (?start=govip_<chat_id>).
       b. Jika TIDAK aktif → skip total, tidak ada respon apapun
          (pesan tetap dihapus secara senyap).

FLOW DI DM (deep-link ?start=govip_<chat_id>):
  Diintersep SEBELUM handler /start umum (plugins/commands/antigcast_group.py)
  lewat group=-1 + pyrogram.ContinuePropagation — jika payload BUKAN
  "govip_...", handler ini melempar ke handler /start biasa tanpa
  mengubah apapun.

  Jika payload valid:
    1. Re-validasi config grup tersebut (jaga-jaga teks VIP sudah
       dimatikan/dihapus admin setelah tombol dibagikan) — jika sudah
       tidak aktif, beri tahu user dengan sopan, bukan diam saja
       (berbeda dari kondisi b di grup, karena di DM user sudah niat
       klik tombol dan menunggu jawaban).
    2. Tampilkan tutorial pasang teks VIP bio (font monospace),
       daftar SEMUA filter/antispam yang akan dilewati di grup itu
       (di-list satu per satu), dan penjelasan mini + tombol untuk
       menambahkan bot ke grup lain dengan kuasa admin penuh.
"""

import time
from html import escape as _html_escape

from pyrogram import Client, ContinuePropagation, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

from database import get_config

# ── Cooldown per grup — 5 menit, blokir SEMUA user di grup yang sama ───────
_group_cooldown: dict[int, float] = {}
_COOLDOWN_SECS = 300   # 5 menit

# Hak admin "penuh" yang diminta saat bot ditambahkan ke grup baru lewat
# tombol di DM — mencakup semua hak yang dipakai fitur-fitur bot
# (antispam, mute/restrict, pin notifikasi, kelola VC untuk Security OS, dll).
_FULL_ADMIN_RIGHTS = (
    "change_info+delete_messages+restrict_members+invite_users"
    "+pin_messages+manage_chat+manage_video_chats+promote_members"
)


def _vip_bio_active(cfg: dict) -> bool:
    """True hanya jika Bio Link Detector ON DAN teks VIP bio sudah diisi."""
    return bool(cfg.get("bio_check")) and bool((cfg.get("bio_vip_text") or "").strip())


# ─────────────────────────────────────────────────────────────────────────────
#  /govip di GRUP
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("govip") & filters.group)
async def cmd_govip(client: Client, message: Message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else None

    # Hapus pesan perintah segera — tidak meninggalkan jejak di grup.
    try:
        await message.delete()
    except Exception:
        pass

    if not uid:
        return

    # ── Cooldown per grup, 5 menit — siapapun yang memicu, grup yang sama
    #    tidak bisa dipicu lagi sampai cooldown habis ─────────────────────
    now = time.time()
    last = _group_cooldown.get(cid, 0.0)
    if now - last < _COOLDOWN_SECS:
        return   # masih cooldown grup → abaikan diam-diam

    cfg = await get_config(cid)
    if not _vip_bio_active(cfg):
        # Teks VIP bio tidak aktif di grup ini → skip total, tidak ada
        # respon apapun (juga tidak menyalakan cooldown, supaya tidak
        # memboroskan jatah 5 menit untuk grup yang fiturnya belum aktif).
        return

    # Set cooldown SEBELUM proses agar tidak ada race saat banyak orang
    # memicu /govip bersamaan persis di detik yang sama.
    _group_cooldown[cid] = now

    try:
        me = await client.get_me()
    except Exception:
        return

    payload   = f"govip_{cid}".replace("-", "n")
    deep_link = f"https://t.me/{me.username}?start={payload}"

    title = _html_escape(message.chat.title or "grup ini")

    text = (
        "⭐ <b>VIP Bio Member</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Mau bebas dari semua filter antispam di <b>{title}</b>?\n"
        "Tekan tombol di bawah, lalu ikuti tutorialnya di chat pribadi bot."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Jadi VIP Member", url=deep_link)],
    ])

    try:
        await client.send_message(
            chat_id=cid,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[GoVIP] Gagal kirim info di grup={cid}: {e}")
        _group_cooldown.pop(cid, None)   # kembalikan jatah cooldown jika gagal kirim


# ─────────────────────────────────────────────────────────────────────────────
#  /start govip_<chat_id> di DM — diintersep sebelum handler /start umum
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("start") & filters.private, group=-1)
async def govip_start_intercept(client: Client, message: Message):
    if len(message.command) < 2 or not message.command[1].startswith("govip_"):
        # Bukan deep-link /govip → lempar ke handler /start umum
        # (plugins/commands/antigcast_group.py), tidak diproses di sini.
        raise ContinuePropagation

    raw_cid = message.command[1][len("govip_"):]
    try:
        cid = int(raw_cid.replace("n", "-", 1)) if raw_cid.startswith("n") else int(raw_cid)
    except ValueError:
        raise ContinuePropagation

    cfg = await get_config(cid)
    if not _vip_bio_active(cfg):
        # Teks VIP bio sudah dimatikan/dihapus admin grup setelah tombol
        # dibagikan — beri tahu user, jangan diamkan (beda dari kondisi
        # di grup, karena di sini user sudah aktif menunggu jawaban).
        await message.reply(
            "⚠️ <b>VIP Bio Member sudah tidak aktif</b>\n\n"
            "Fitur ini baru saja dimatikan oleh admin grup, atau teks VIP "
            "bionya sudah dihapus. Coba lagi nanti, atau hubungi admin grup.",
            parse_mode=ParseMode.HTML,
        )
        return

    vip_text = _html_escape((cfg.get("bio_vip_text") or "").strip())

    try:
        chat = await client.get_chat(cid)
        group_name = _html_escape(chat.title or "grup tersebut")
    except Exception:
        group_name = "grup tersebut"

    # ── Daftar SEMUA filter/antispam yang akan dilewati, satu per satu ─────
    bypass_list = (
        "1️⃣ Filter Kata (Regex Global &amp; Lokal)\n"
        "2️⃣ Anti-Mention (mention dari luar grup)\n"
        "3️⃣ Bio Link Detector\n"
        "4️⃣ Anti-Spam Duplikasi Lokal (pesan berulang)\n"
        "5️⃣ Anti-GCast (broadcast massal lintas grup)\n"
        "6️⃣ CAS Global (auto-ban spammer terverifikasi)\n"
        "7️⃣ Nexus AI &amp; Filter Kata Otomatis\n"
        "8️⃣ Mute Mic Otomatis (Security OS — Obrolan Suara)"
    )

    try:
        me = await client.get_me()
        add_url = f"https://t.me/{me.username}?startgroup=true&admin={_FULL_ADMIN_RIGHTS}"
    except Exception:
        add_url = None

    text = (
        "⭐ <b>Cara Jadi VIP Member</b>\n"
        f"<code>Grup: {group_name}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pasang teks berikut di <b>bio profil Telegram</b> kamu "
        "(boleh ada teks lain juga, asal teks ini ikut tercantum):\n\n"
        f"<code>{vip_text}</code>\n\n"
        "Begitu bot mendeteksi teks itu di bio kamu, status VIP aktif "
        "otomatis — tidak perlu lapor admin.\n\n"
        "<b>🛡️ Sebagai VIP, kamu bebas dari:</b>\n"
        f"{bypass_list}\n\n"
        "<i>aktifkan bot ini di grupmu</i>"
    )

    keyboard = None
    if add_url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Tambahkan Bot ke Grup", url=add_url)],
        ])

    await message.reply(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

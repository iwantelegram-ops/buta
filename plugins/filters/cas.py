"""
plugins/filters/cas.py
──────────────────────
Filter CAS (Combot Anti-Spam):
  - Auto-ban user yang ada di database spammer global CAS
  - Kirim sambutan saat bot masuk grup baru
  - Perintah /wlcas dan /unwlcas untuk whitelist per grup

PINTU BERURUTAN:
  Jika CAS mendeteksi dan mem-ban user → mark_message_handled(cid, mid)
  sebelum memasukkan ke delete_queue, sehingga bio, antispam, dan nexus
  tidak memproses pesan yang sama.

VIP:
  User yang terdaftar di free_per_group sepenuhnya dilewati — tidak dicek
  sama sekali, tidak ada ban, tidak ada log.
"""

import os
import httpx
import time
import asyncio
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode

from database import (
    db, auto_delete_reply, is_admin, delete_queue,
    update_config, save_group_title, save_group_username, remove_group_data,
    TZ_WIB, mark_message_handled, insert_group_action_log, get_config,
    check_bot_permissions,
)
from core.group_notify import send_group_notice
from core.moderation_queue import queue_ban

DELAY_NOTIF   = 10
LOG_CHANNEL   = int(os.environ.get("LOG_CHANNEL", 0))
whitelist_col = db["whitelist_per_group"]
free_col      = db["free_per_group"]

_cas_cache: dict[int, tuple[bool, float]] = {}
_CAS_CACHE_TTL = 3600  


async def check_cas_global(user_id: int) -> bool:
    now = time.monotonic()
    if user_id in _cas_cache:
        is_banned, ts = _cas_cache[user_id]
        if now - ts < _CAS_CACHE_TTL:
            return is_banned

    url = f"https://api.combot.org/cas/check?user_id={user_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                ok = data.get("ok", False)
                _cas_cache[user_id] = (ok, now)
                return ok
    except Exception as e:
        print(f"[CAS API Error] {e}")
    return False


@Client.on_message(filters.command(["wlcas", "unwlcas"]) & filters.group)
async def control_cas_whitelist(client: Client, message: Message):
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else 0

    if not uid:
        return

    if not await is_admin(client, cid, uid):
        return

    cmd = message.command[0].lower()
    if cmd == "wlcas":
        await whitelist_col.update_one({"chat_id": cid}, {"$set": {"cas_disabled": True}}, upsert=True)
        rep = await message.reply("✅ <b>CAS Whitelist diaktifkan.</b> Spammer CAS global tidak akan di-ban di grup ini.")
    else:
        await whitelist_col.delete_one({"chat_id": cid})
        rep = await message.reply("❌ <b>CAS Whitelist dicabut.</b> Spammer CAS global akan otomatis di-ban.")

    await auto_delete_reply(message, rep)


@Client.on_message(filters.group & ~filters.service, group=-1)
async def cas_filter(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    cid = message.chat.id
    uid = message.from_user.id
    mid = message.id

    # ── PENGECEKAN IZIN BOT: Menutup mata jika tidak ada hak delete & ban ──
    if not await check_bot_permissions(client, cid):
        return

    if await free_col.find_one({"user_id": uid, "chat_id": cid}):
        return

    wl = await whitelist_col.find_one({"chat_id": cid})
    if wl and wl.get("cas_disabled"):
        return

    if await is_admin(client, cid, uid):
        return

    is_banned = await check_cas_global(uid)
    if is_banned:
        mark_message_handled(cid, mid)
        await delete_queue.put((cid, [mid]))

        user_name = message.from_user.first_name or str(uid)
        waktu = datetime.now(TZ_WIB).strftime("%d/%m/%Y %H:%M:%S WIB")

        log_text = (
            "<b>❖ COMBOT ANTI-SPAM (CAS) ❖</b>\n"
            "🔨 <b>Tindakan: Ban Otomatis</b>\n"
            "<blockquote>"
            f"◈ <b>User:</b> <a href='tg://user?id={uid}'>{user_name}</a> (<code>{uid}</code>)\n"
            f"◈ <b>Grup:</b> {message.chat.title} (<code>{cid}</code>)\n"
            f"◈ <b>Waktu:</b> {waktu}\n"
            "◈ <b>Alasan:</b> Terdeteksi di Database Spammer Global CAS"
            "</blockquote>"
        )

        try:
            await insert_group_action_log(
                cid, "BAN",
                "Terdeteksi di Database Spammer Global CAS",
                uid, user_name,
                (message.text or message.caption or "")[:100]
            )
        except Exception:
            pass

        async def _on_done(success: bool):
            if success and LOG_CHANNEL:
                try:
                    await client.send_message(LOG_CHANNEL, log_text, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

        await queue_ban(cid, uid, on_done=_on_done)


# ── Pantau perubahan status bot di grup ──────────────────────────────────────
@Client.on_chat_member_updated()
async def handle_bot_status_change(client: Client, update):
    try:
        me = await client.get_me()
        if not update.new_chat_member or update.new_chat_member.user.id != me.id:
            return

        from pyrogram.enums import ChatMemberStatus
        new_status = update.new_chat_member.status
        chat_id    = update.chat.id

        if new_status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            await remove_group_data(chat_id)

        elif new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
            # ── PENGECEKAN IZIN BOT: Jika status berubah tapi izin tidak lengkap, skip ──
            if not await check_bot_permissions(client, chat_id):
                return

            await update_config(chat_id, "local", True)
            try:
                chat = await client.get_chat(chat_id)
                await save_group_title(chat_id, chat.title or str(chat_id))
                await save_group_username(chat_id, getattr(chat, "username", None))
            except Exception:
                pass

    except Exception as e:
        print(f"[handle_bot_status_change] {e}")


# ── Nama grup berubah → perbarui title di database ───────────────────────────
@Client.on_message(filters.group & filters.service)
async def handle_chat_title_change(client: Client, message: Message):
    try:
        if message.new_chat_title:
            # Tambahan pengaman opsional pada event service ganti nama
            if not await check_bot_permissions(client, message.chat.id):
                return
            await save_group_title(message.chat.id, message.new_chat_title)
    except Exception:
        pass

"""
userbot.py — Pyrogram user-client logic
  • List joined channels
  • Resolve any channel link (public, private invite, @username, numeric ID)
  • Forward messages (copy, no "Forwarded" tag, optional custom caption + thumbnail)
"""
from __future__ import annotations
import asyncio, re, os
from typing import Callable, Awaitable

from pyrogram import Client
from pyrogram.raw.functions.messages import CheckChatInvite
from pyrogram.errors import (
    FloodWait,
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    ChatAdminRequired,
    ChannelPrivate,
    MessageIdInvalid,
    PeerIdInvalid,
)
from pyrogram.types import Chat, Message

import config

# ─────────────────────────────  Client factory  ──────────────────────────────

# Pyrogram needs api_id/api_hash even when a session_string is provided.
# The auth key is embedded in the session string itself, so any valid pair works.
_DEFAULT_API_ID   = 2
_DEFAULT_API_HASH = "36722c72256a24c1225de00eb6a1ca74"


def _make_client(
    api_id: int | None = None,
    api_hash: str | None = None,
    session_string: str | None = None,
) -> Client:
    return Client(
        name="autoforward_user",
        api_id=api_id or _DEFAULT_API_ID,
        api_hash=api_hash or _DEFAULT_API_HASH,
        session_string=session_string,
        in_memory=True,
        no_updates=True,  # Crucial to prevent hang on connect
    )



# ─────────────────────────────  URL / ID parsing  ────────────────────────────

def _parse_link(text: str) -> tuple[str, str | int]:
    """
    Categorize and extract the target from any input string.
    Returns: (type, target) where type is "invite", "id", or "username".
    """
    text = text.strip()

    # 1. Numeric ID
    if re.match(r"^-?\d+$", text):
        return ("id", int(text))

    # 2. Invite links (joinchat/Hash or +Hash)
    m_invite = re.search(r"(?:joinchat/|\+)([\w-]+)", text)
    if m_invite:
        return ("invite", m_invite.group(1))

    # 3. Public t.me/username links
    m_user = re.search(r"t\.me/([\w_]+)", text)
    if m_user:
        return ("username", m_user.group(1))

    # 4. Fallback: treat as raw username (with or without @)
    return ("username", text.lstrip("@"))


# ─────────────────────────────  Channel helpers  ─────────────────────────────

async def get_joined_channels(session_string: str) -> list[dict]:
    """Return list of {id, title, username, type} for all joined channels/groups. Capped at 500."""
    results = []
    async with _make_client(session_string=session_string) as client:
        async for dialog in client.get_dialogs(limit=500):
            chat: Chat = dialog.chat
            if chat.type.name in ("CHANNEL", "SUPERGROUP", "GROUP"):
                results.append({
                    "id": chat.id,
                    "title": chat.title or "Unnamed",
                    "username": f"@{chat.username}" if chat.username else str(chat.id),
                    "type": chat.type.name,
                })
    return results


async def resolve_and_join_channel(session_string: str, link_or_username: str) -> dict:
    """
    Resolve any channel/group reference to {id, title, username}.
    Supports: @username, t.me/username, t.me/+hash, https://t.me/joinchat/hash, -100xxx.
    Auto-joins if not already a member.
    Raises ValueError with a user-friendly message on failure.
    """
    async with _make_client(session_string=session_string) as client:
        target_type, target_val = _parse_link(link_or_username)

        # ── Private invite hash ───────────────────────────────────────────────
        if target_type == "invite":
            invite_hash = target_val
            full_invite_link = f"https://t.me/+{invite_hash}"
            try:
                chat = await client.join_chat(full_invite_link)
                return _chat_dict(chat)
            except UserAlreadyParticipant:
                pass
            except InviteHashExpired:
                raise ValueError("❌ This invite link has expired.")
            except InviteHashInvalid:
                raise ValueError("❌ This invite link is invalid.")
            except FloodWait as e:
                if e.value > 30:
                    raise ValueError(f"❌ Telegram rate limit is too high ({e.value}s). Please try again later.")
                await asyncio.sleep(e.value + 1)
                try:
                    chat = await client.join_chat(full_invite_link)
                    return _chat_dict(chat)
                except Exception as e2:
                    raise ValueError(f"❌ Telegram rate limit retry failed: {e2}")
            except Exception as e:
                raise ValueError(f"❌ Could not join channel: {e}")

            # Already a member — get info via raw CheckChatInvite
            try:
                inv = await client.invoke(CheckChatInvite(hash=invite_hash))
                raw_chat = getattr(inv, "chat", None)
                if raw_chat is not None:
                    rid = getattr(raw_chat, "id", None)
                    title = getattr(raw_chat, "title", str(rid))
                    uname = getattr(raw_chat, "username", None)
                    if rid:
                        # Raw Pyrogram channel IDs don't carry the -100 prefix
                        peer_id = int(f"-100{rid}") if rid > 0 else rid
                        return {
                            "id": peer_id,
                            "title": title,
                            "username": f"@{uname}" if uname else str(peer_id),
                        }
            except Exception:
                pass

            raise ValueError(
                "❌ Already a member but couldn't fetch channel info.\n\n"
                "Please try with:\n• `@username`\n• Numeric ID: `-100xxxxxxxxxx`"
            )

        # ── Numeric ID ────────────────────────────────────────────────────────
        elif target_type == "id":
            chat_id = int(target_val)
            if 0 < chat_id < 1_000_000_000_000:
                chat_id = int(f"-100{chat_id}")
            try:
                chat = await client.get_chat(chat_id)
                return _chat_dict(chat)
            except FloodWait as e:
                if e.value > 30:
                    raise ValueError(f"❌ Telegram rate limit is too high ({e.value}s). Please try again later.")
                await asyncio.sleep(e.value + 1)
                try:
                    chat = await client.get_chat(chat_id)
                    return _chat_dict(chat)
                except Exception as e2:
                    raise ValueError(f"❌ Telegram rate limit retry failed: {e2}")
            except Exception as e:
                raise ValueError(f"❌ Could not fetch channel by ID `{chat_id}`: {e}")

        # ── @username or plain username ────────────────────────────────────────
        elif target_type == "username":
            username_clean = target_val
            try:
                chat = await client.get_chat(f"@{username_clean}")
            except ChannelPrivate:
                raise ValueError(
                    "❌ This channel is private.\n"
                    "Provide an invite link: `https://t.me/+hash`"
                )
            except FloodWait as e:
                if e.value > 30:
                    raise ValueError(f"❌ Telegram rate limit is too high ({e.value}s). Please try again later.")
                await asyncio.sleep(e.value + 1)
                try:
                    chat = await client.get_chat(f"@{username_clean}")
                except Exception as e2:
                    raise ValueError(f"❌ Telegram rate limit retry failed: {e2}")
            except Exception as e:
                raise ValueError(f"❌ Could not find `@{username_clean}`: {e}")

            # Try to join (silently ignore if already a member or if we're an admin)
            try:
                await client.join_chat(f"@{username_clean}")
            except FloodWait as e:
                if e.value <= 30:
                    await asyncio.sleep(e.value + 1)
                    try:
                        await client.join_chat(f"@{username_clean}")
                    except Exception:
                        pass
            except (UserAlreadyParticipant, Exception):
                pass

            return _chat_dict(chat)


def _chat_dict(chat) -> dict:
    return {
        "id": chat.id,
        "title": chat.title or str(chat.id),
        "username": f"@{chat.username}" if chat.username else str(chat.id),
    }


async def get_latest_message_id(session_string: str, channel_id: int) -> int | None:
    """
    Return the ID of the most recent message in a channel.
    Uses get_dialogs so the peer cache is populated as a side effect.
    """
    async with _make_client(session_string=session_string) as client:
        try:
            async for dialog in client.get_dialogs(limit=500):
                if dialog.chat.id == channel_id:
                    if dialog.top_message:
                        return dialog.top_message.id
                    return None
        except Exception:
            pass
    return None


ProgressCallback = Callable[[int, int, int], Awaitable[None]]


async def forward_messages(
    session_string: str,
    source: str | int,
    destinations: list[str | int],
    start_id: int,
    end_id: int,
    caption: str | None,
    thumbnail_path: str | None = None,
    progress_cb: ProgressCallback | None = None,
    stop_event: asyncio.Event | None = None,
    source_ref: str | None = None,       # original text typed by user (for fallback resolve)
    dest_refs: list[str] | None = None,  # username/link per destination (for fallback resolve)
) -> dict:
    """
    Copy messages without 'Forwarded from' header using copy_message().
    caption=None  → keep original caption on media; text messages unchanged
    caption=''    → strip caption from media; text messages unchanged
    caption='...' → replace caption on media only; text messages unchanged
    thumbnail_path → replace thumbnail on video/document only
    Returns {forwarded, errors, skipped}.
    """
    forwarded = 0
    errors = 0
    skipped = 0
    total = end_id - start_id + 1
    last_error: str | None = None

    # Message types that can carry a caption
    MEDIA_TYPES = ("video", "photo", "audio", "document", "animation", "voice", "video_note", "sticker")

    async with _make_client(session_string=session_string) as client:
        # ── Warm up peer cache via dialogs ────────────────────────────────────────
        needed_ids: set = {source} | {d for d in destinations}
        found_ids: set = set()
        try:
            async for dialog in client.get_dialogs(limit=1000):  # scan top 1000 dialogs
                if dialog.chat.id in needed_ids:
                    found_ids.add(dialog.chat.id)
                if found_ids >= needed_ids:
                    break
        except Exception:
            pass

        # ── Fallback: resolve any unfound peers by username/link ───────────────────
        missing = needed_ids - found_ids
        if missing:
            refs_to_try: list[str] = []
            if source in missing and source_ref:
                refs_to_try.append(source_ref)
            if dest_refs:
                for idx, d_id in enumerate(destinations):
                    if d_id in missing and idx < len(dest_refs) and dest_refs[idx]:
                        refs_to_try.append(dest_refs[idx])

            for ref in refs_to_try:
                try:
                    t, v = _parse_link(ref)
                    if t == "invite":
                        try:
                            await client.join_chat(f"https://t.me/+{v}")
                        except UserAlreadyParticipant:
                            # Already a member — use CheckChatInvite to populate peer cache
                            try:
                                await client.invoke(CheckChatInvite(hash=v))
                            except Exception:
                                pass
                    elif t == "username":
                        try:
                            await client.get_chat(f"@{v}")
                        except Exception:
                            pass
                except Exception:
                    pass

        for msg_id in range(start_id, end_id + 1):
            if stop_event and stop_event.is_set():
                break

            # ── Fetch ─────────────────────────────────────────────────────────
            try:
                msg: Message = await client.get_messages(source, msg_id)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                try:
                    msg = await client.get_messages(source, msg_id)
                except Exception as e2:
                    errors += 1; last_error = str(e2); continue
            except MessageIdInvalid:
                skipped += 1; continue
            except Exception as e:
                errors += 1; last_error = str(e); continue

            if msg is None or msg.empty:
                skipped += 1
                continue

            # ── Determine message type ─────────────────────────────────────────
            is_text_only = (
                msg.text is not None and
                msg.video is None and msg.photo is None and
                msg.audio is None and msg.document is None and
                msg.animation is None and msg.voice is None and
                msg.sticker is None and msg.poll is None
            )
            is_media = any(getattr(msg, t, None) is not None for t in MEDIA_TYPES)
            has_video = msg.video is not None
            has_document = msg.document is not None
            can_have_thumbnail = has_video or has_document

            # ── Caption logic ─────────────────────────────────────────────────
            if is_text_only:
                effective_caption = None
                override_caption = False
            elif is_media and caption is not None:
                effective_caption = caption if caption else None
                override_caption = True
            else:
                effective_caption = msg.caption or None
                override_caption = False

            # Sent as plain text to avoid Markdown errors on special characters
            parse_mode = None

            # ── Send to each destination ──────────────────────────────────────
            sent_ok = 0
            for dest in destinations:

                async def _send_to(dest_id):
                    """Inner helper — raises on error so caller can retry."""
                    if thumbnail_path and can_have_thumbnail:
                        # Download original → re-upload with custom thumbnail
                        dl_path = None
                        try:
                            dl_path = await client.download_media(msg, in_memory=False)
                            if dl_path is None:
                                raise ValueError("download_media returned None")
                            if has_video:
                                await client.send_video(
                                    chat_id=dest_id,
                                    video=dl_path,
                                    caption=effective_caption,
                                    thumb=thumbnail_path,
                                )
                            else:
                                await client.send_document(
                                    chat_id=dest_id,
                                    document=dl_path,
                                    caption=effective_caption,
                                    thumb=thumbnail_path,
                                )
                        finally:
                            if dl_path:
                                try:
                                    os.remove(dl_path)
                                except Exception:
                                    pass

                    elif override_caption:
                        # Media with custom caption only (no thumbnail)
                        await client.copy_message(
                            chat_id=dest_id,
                            from_chat_id=source,
                            message_id=msg_id,
                            caption=effective_caption,
                        )

                    else:
                        # Text or media with no overrides — copy exactly as-is
                        await client.copy_message(
                            chat_id=dest_id,
                            from_chat_id=source,
                            message_id=msg_id,
                        )

                try:
                    await _send_to(dest)
                    sent_ok += 1

                except PeerIdInvalid:
                    # Peer not in cache — scan ALL dialogs to force-resolve it
                    try:
                        async for dialog in client.get_dialogs():
                            if dialog.chat.id == dest:
                                break
                    except Exception:
                        pass
                    # Retry once after forced resolution
                    try:
                        await _send_to(dest)
                        sent_ok += 1
                    except Exception as e2:
                        errors += 1
                        last_error = str(e2)

                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    try:
                        await _send_to(dest)
                        sent_ok += 1
                    except Exception as e2:
                        errors += 1
                        last_error = str(e2)

                except ChatAdminRequired:
                    errors += 1
                    last_error = f"No posting rights in destination {dest}"

                except Exception as e:
                    errors += 1
                    last_error = str(e)

            if sent_ok > 0:
                forwarded += 1

            if progress_cb and (forwarded % max(1, config.PROGRESS_INTERVAL) == 0):
                await progress_cb(forwarded, total, errors)

            await asyncio.sleep(0.5)

    result = {"forwarded": forwarded, "errors": errors, "skipped": skipped}
    if last_error:
        result["last_error"] = last_error  # always report last error
    return result


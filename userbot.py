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
    )


# ─────────────────────────────  URL / ID parsing  ────────────────────────────

def _parse_link(text: str) -> str:
    """
    Extract the meaningful part from any Telegram channel reference.
    Returns: '+hash', '@username', '-100xxxxxxxx' (as string), or plain 'username'.
    """
    text = text.strip()
    # Full URL
    m = re.match(r"https?://t\.me/(?:joinchat/)?([\w+/=@-]+)", text)
    if m:
        return m.group(1)
    # t.me/... without scheme
    m = re.match(r"t\.me/(?:joinchat/)?([\w+/=@-]+)", text)
    if m:
        return m.group(1)
    # Already bare: @username, +hash, -100xxx, or plain username
    return text


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
    Supports: @username, t.me/username, t.me/+hash, https://t.me/+hash, -100xxx, plain numeric.
    Auto-joins if not already a member.
    Raises ValueError with a user-friendly message on failure.
    """
    async with _make_client(session_string=session_string) as client:
        target = _parse_link(link_or_username)

        # ── Private invite hash (+abc123) ─────────────────────────────────────
        if target.startswith("+"):
            invite_hash = target.lstrip("+")
            try:
                chat = await client.join_chat(target)
                return _chat_dict(chat)
            except UserAlreadyParticipant:
                pass
            except InviteHashExpired:
                raise ValueError("❌ This invite link has expired.")
            except InviteHashInvalid:
                raise ValueError("❌ This invite link is invalid.")
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                raise ValueError("❌ Telegram rate limit. Please try again in a moment.")
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
        if re.match(r"^-?\d+$", target):
            chat_id = int(target)
            if 0 < chat_id < 1_000_000_000_000:
                chat_id = int(f"-100{chat_id}")
            try:
                chat = await client.get_chat(chat_id)
                return _chat_dict(chat)
            except Exception as e:
                raise ValueError(f"❌ Could not fetch channel by ID `{chat_id}`: {e}")

        # ── @username or plain username ────────────────────────────────────────
        username_clean = target.lstrip("@")
        try:
            chat = await client.get_chat(f"@{username_clean}")
        except ChannelPrivate:
            raise ValueError(
                "❌ This channel is private.\n"
                "Provide an invite link: `https://t.me/+hash`"
            )
        except Exception as e:
            raise ValueError(f"❌ Could not find `@{username_clean}`: {e}")

        # Try to join (silently ignore if already a member or if we're an admin)
        try:
            await client.join_chat(f"@{username_clean}")
        except (UserAlreadyParticipant, Exception):
            pass

        return _chat_dict(chat)


def _chat_dict(chat) -> dict:
    return {
        "id": chat.id,
        "title": chat.title or str(chat.id),
        "username": f"@{chat.username}" if chat.username else str(chat.id),
    }


# ─────────────────────────────  Forward engine  ──────────────────────────────

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
            # Text-only messages always go through unchanged.
            # For media: apply custom caption if set, else keep original.
            if is_text_only:
                # Pure text — send without any caption override
                effective_caption = None
                override_caption = False
            elif is_media and caption is not None:
                # Media + user provided a caption override
                effective_caption = caption if caption else None
                override_caption = True
            else:
                # Media without override — keep original
                effective_caption = msg.caption or None
                override_caption = False

            parse_mode = "markdown" if effective_caption else None

            # ── Send to all destinations ──────────────────────────────────────
            sent_ok = 0
            for dest in destinations:
                try:
                    if thumbnail_path and can_have_thumbnail:
                        # Download → re-upload with custom thumbnail
                        try:
                            dl_path = await client.download_media(msg, in_memory=False)
                            if has_video:
                                await client.send_video(
                                    chat_id=dest,
                                    video=dl_path,
                                    caption=effective_caption,
                                    parse_mode=parse_mode,
                                    thumb=thumbnail_path,
                                )
                            else:
                                await client.send_document(
                                    chat_id=dest,
                                    document=dl_path,
                                    caption=effective_caption,
                                    parse_mode=parse_mode,
                                    thumb=thumbnail_path,
                                )
                            try:
                                os.remove(dl_path)
                            except Exception:
                                pass
                        except Exception:
                            # Thumbnail failed — fall back to regular copy
                            await client.copy_message(
                                chat_id=dest,
                                from_chat_id=source,
                                message_id=msg_id,
                                caption=effective_caption if override_caption else msg.caption,
                                parse_mode=parse_mode,
                            )
                    elif override_caption and is_media:
                        # Media with custom caption — use copy_message with override
                        await client.copy_message(
                            chat_id=dest,
                            from_chat_id=source,
                            message_id=msg_id,
                            caption=effective_caption,
                            parse_mode=parse_mode,
                        )
                    else:
                        # Text or media with no override — copy as-is (no caption param)
                        await client.copy_message(
                            chat_id=dest,
                            from_chat_id=source,
                            message_id=msg_id,
                        )
                    sent_ok += 1
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    try:
                        await client.copy_message(
                            chat_id=dest,
                            from_chat_id=source,
                            message_id=msg_id,
                            caption=effective_caption if override_caption else None,
                            parse_mode=parse_mode if override_caption else None,
                        )
                        sent_ok += 1
                    except Exception as e2:
                        errors += 1; last_error = str(e2)
                except ChatAdminRequired:
                    raise ValueError(
                        f"❌ No posting rights in destination `{dest}`.\n"
                        "Make sure the user account has 'Post Messages' permission."
                    )
                except Exception as e:
                    errors += 1; last_error = str(e)

            if sent_ok > 0:
                forwarded += 1

            if progress_cb and (forwarded % max(1, config.PROGRESS_INTERVAL) == 0):
                await progress_cb(forwarded, total, errors)

            await asyncio.sleep(0.5)

    result = {"forwarded": forwarded, "errors": errors, "skipped": skipped}
    if last_error and forwarded == 0:
        result["last_error"] = last_error
    return result


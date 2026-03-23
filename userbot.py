"""
userbot.py — Pyrogram user-client logic
  • Login (phone → OTP → optional 2FA)
  • List joined channels
  • Join channel from invite link or username
  • Forward messages (copy, no "Forwarded" tag, optional custom caption)
"""
from __future__ import annotations
import asyncio, re
from typing import Callable, Awaitable

from pyrogram import Client
from pyrogram.raw.functions.messages import CheckChatInvite
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
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


# ─────────────────────────────  Helpers  ─────────────────────────────────────

import os
import uuid

# Default fallback values — Pyrogram needs non-empty api_id/api_hash to construct
# the Client even when a session_string is provided (auth_key is embedded in the string).
_DEFAULT_API_ID = 2
_DEFAULT_API_HASH = "36722c72256a24c1225de00eb6a1ca74"

def _make_client(
    api_id: int | None = None,
    api_hash: str | None = None,
    session_string: str | None = None,
) -> Client:
    """Create a Pyrogram in-memory client. api_id/api_hash are optional when session_string is supplied."""
    return Client(
        name="autoforward_user",
        api_id=api_id or _DEFAULT_API_ID,
        api_hash=api_hash or _DEFAULT_API_HASH,
        session_string=session_string,
        in_memory=True,
        device_model="PC",
        system_version="Windows 10",
        app_version="5.1.1",
        lang_code="en",
    )


def _channel_id_from_text(text: str) -> str:
    """Strip URL fluff to leave username or +hash."""
    text = text.strip()
    m = re.match(r"https?://t\.me/(?:joinchat/)?(\S+)", text)
    if m:
        return m.group(1)
    if text.startswith("@"):
        return text
    return text


# ─────────────────────────────  Login flow  ──────────────────────────────────

class LoginSession:
    """Stateful object that tracks the OTP/2FA flow."""

    def __init__(self):
        self.client: Client | None = None
        self.phone: str = ""
        self.phone_code_hash: str = ""
        self.api_id: int = 0
        self.api_hash: str = ""

    async def start_login(self, phone: str, api_id: int, api_hash: str) -> str:
        """Start login, returns OTP type string (e.g. 'APP' or 'SMS')."""
        self.phone = phone.strip()
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = _make_client(api_id, api_hash)
        await self.client.connect()
        sent = await self.client.send_code(self.phone)
        self.phone_code_hash = sent.phone_code_hash
        return sent.type.name

    async def submit_code(self, code: str) -> tuple[bool, str]:
        """
        Submit OTP. Returns (needs_password, session_string).
        If needs_password is True, call submit_password next.
        """
        try:
            await self.client.sign_in(
                phone_number=self.phone,
                phone_code_hash=self.phone_code_hash,
                phone_code=code.strip(),
            )
            session_string = await self.client.export_session_string()
            await self.cancel()
            return False, session_string
        except SessionPasswordNeeded:
            return True, ""
        except PhoneCodeInvalid:
            raise ValueError("❌ The OTP code you entered is incorrect.")
        except PhoneCodeExpired:
            raise ValueError("❌ The OTP code has expired. Please restart login.")

    async def submit_password(self, password: str) -> str:
        """Check 2FA password. Returns session_string."""
        await self.client.check_password(password.strip())
        session_string = await self.client.export_session_string()
        await self.cancel()
        return session_string

    async def cancel(self):
        """Disconnect and cleanup temporary session files."""
        if self.client:
            name = self.client.name
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
            if name and name.startswith("login_"):
                for ext in (".session", ".session-journal"):
                    try:
                        os.remove(f"{name}{ext}")
                    except OSError:
                        pass


# ─────────────────────────────  Channel helpers  ─────────────────────────────

async def get_joined_channels(session_string: str) -> list[dict]:
    """Return list of dicts: {id, title, username, type}. Fast — caps at 500 dialogs."""
    results = []
    async with _make_client(session_string=session_string) as client:
        async for dialog in client.get_dialogs(limit=500):
            chat: Chat = dialog.chat
            if chat.type.name in ("CHANNEL", "SUPERGROUP", "GROUP"):
                results.append(
                    {
                        "id": chat.id,
                        "title": chat.title or "Unnamed",
                        "username": f"@{chat.username}" if chat.username else str(chat.id),
                        "type": chat.type.name,
                    }
                )
    return results


async def resolve_and_join_channel(session_string: str, link_or_username: str) -> dict:
    """
    Resolve a channel from username / link / invite link / numeric ID and auto-join if needed.
    Returns {id, title, username}. Raises ValueError with user-friendly message on error.
    Accepts: @username, t.me/username, t.me/+hash, https://t.me/... , -100xxxxxxxx
    """
    async with _make_client(session_string=session_string) as client:
        target = _channel_id_from_text(link_or_username)

        # ── Private invite link (starts with +) ──────────────────────────────
        if target.startswith("+"):
            try:
                chat = await client.join_chat(target)
                return {
                    "id": chat.id,
                    "title": chat.title,
                    "username": f"@{chat.username}" if chat.username else str(chat.id),
                }
            except UserAlreadyParticipant:
                # Already a member — get info via check_invite_link
                try:
                    link_info = await client.get_chat(f"t.me/{target}")
                    return {
                        "id": link_info.id,
                        "title": link_info.title,
                        "username": f"@{link_info.username}" if link_info.username else str(link_info.id),
                    }
                except Exception:
                    # Fallback: list dialogs to find the channel we're already in
                    try:
                        inv = await client.invoke(
                            CheckChatInvite(hash=target.lstrip("+"))
                        )
                        chat_obj = getattr(inv, "chat", None)
                        if chat_obj:
                            return {
                                "id": chat_obj.id,
                                "title": getattr(chat_obj, "title", str(chat_obj.id)),
                                "username": f"@{chat_obj.username}" if getattr(chat_obj, "username", None) else str(chat_obj.id),
                            }
                    except Exception:
                        pass
                    raise ValueError("❌ Already a member of this channel but couldn't fetch its info. Try using its @username instead.")
            except InviteHashExpired:
                raise ValueError("❌ This invite link has expired.")
            except InviteHashInvalid:
                raise ValueError("❌ This invite link is invalid.")
            except Exception as e:
                raise ValueError(f"❌ Could not join via invite link: {e}")

        # ── Public username / numeric ID ──────────────────────────────────────
        try:
            chat = await client.get_chat(target)
        except ChannelPrivate:
            raise ValueError("❌ This channel is private. Please provide an invite link (t.me/+...).")
        except Exception as e:
            raise ValueError(f"❌ Could not resolve channel: {e}")

        try:
            await client.join_chat(target)
        except UserAlreadyParticipant:
            pass
        except Exception:
            pass  # Already a member or admin — no need to join

        return {
            "id": chat.id,
            "title": chat.title,
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
    Copy messages without 'Forwarded' tag using copy_message().
    - caption: None = keep original, '' = remove caption, any string = override
    - thumbnail_path: local path to image to use as video thumbnail
    Returns {forwarded, errors, skipped}.
    """
    forwarded = 0
    errors = 0
    skipped = 0
    total = end_id - start_id + 1

    async with _make_client(session_string=session_string) as client:
        for msg_id in range(start_id, end_id + 1):
            if stop_event and stop_event.is_set():
                break

            # ── Fetch message ──────────────────────────────────────────────────
            try:
                msg: Message = await client.get_messages(source, msg_id)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                try:
                    msg = await client.get_messages(source, msg_id)
                except Exception:
                    errors += 1
                    continue
            except MessageIdInvalid:
                skipped += 1
                continue
            except Exception:
                errors += 1
                continue

            if msg is None or msg.empty:
                skipped += 1
                continue

            # ── Determine effective caption ────────────────────────────────────
            if caption is not None:
                # User set a caption override (even empty string means "no caption")
                effective_caption = caption or None
            else:
                # Keep original
                effective_caption = msg.caption or msg.text or None

            # ── Is this a video that can have a thumbnail replaced? ────────────
            is_video = msg.video is not None or msg.document is not None

            # ── Send to all destinations ───────────────────────────────────────
            sent_ok = 0
            for dest in destinations:
                try:
                    if thumbnail_path and is_video:
                        # Download media, re-upload with new thumbnail
                        try:
                            dl_path = await client.download_media(msg, in_memory=False)
                            if msg.video:
                                await client.send_video(
                                    chat_id=dest,
                                    video=dl_path,
                                    caption=effective_caption,
                                    parse_mode="markdown" if effective_caption else None,
                                    thumb=thumbnail_path,
                                )
                            else:
                                await client.send_document(
                                    chat_id=dest,
                                    document=dl_path,
                                    caption=effective_caption,
                                    parse_mode="markdown" if effective_caption else None,
                                    thumb=thumbnail_path,
                                )
                            try:
                                import os; os.remove(dl_path)
                            except Exception:
                                pass
                        except FloodWait as e:
                            await asyncio.sleep(e.value + 1)
                            errors += 1
                            continue
                        except Exception:
                            # Fallback: copy without thumbnail
                            await client.copy_message(
                                chat_id=dest,
                                from_chat_id=source,
                                message_id=msg_id,
                                caption=effective_caption,
                                parse_mode="markdown" if effective_caption else None,
                            )
                    else:
                        await client.copy_message(
                            chat_id=dest,
                            from_chat_id=source,
                            message_id=msg_id,
                            caption=effective_caption,
                            parse_mode="markdown" if effective_caption else None,
                        )
                    sent_ok += 1
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    try:
                        await client.copy_message(
                            chat_id=dest,
                            from_chat_id=source,
                            message_id=msg_id,
                            caption=effective_caption,
                            parse_mode="markdown" if effective_caption else None,
                        )
                        sent_ok += 1
                    except Exception:
                        errors += 1
                except ChatAdminRequired:
                    raise ValueError(f"❌ Account needs posting rights in destination: {dest}")
                except Exception:
                    errors += 1

            # Only count as forwarded if at least one destination succeeded
            if sent_ok > 0:
                forwarded += 1
            elif sent_ok == 0 and destinations:
                # All destinations failed for this message
                errors += 1
                skipped -= 1  # Don't double-count

            if progress_cb and forwarded % config.PROGRESS_INTERVAL == 0:
                await progress_cb(forwarded, total, errors)

            await asyncio.sleep(0.5)

    return {"forwarded": forwarded, "errors": errors, "skipped": skipped}


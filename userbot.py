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

def _make_client(api_id: int, api_hash: str, session_string: str | None = None) -> Client:
    """Create a Pyrogram in-memory client spoofing as an official app."""
    return Client(
        name="autoforward_user",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        in_memory=True,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="5.0.3",
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
        return await self.client.export_session_string()

    async def cancel(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None


# ─────────────────────────────  Channel helpers  ─────────────────────────────

async def get_joined_channels(session_string: str, api_id: int, api_hash: str) -> list[dict]:
    """Return list of dicts: {id, title, username, type}."""
    results = []
    async with _make_client(api_id, api_hash, session_string) as client:
        async for dialog in client.get_dialogs():
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


async def resolve_and_join_channel(
    session_string: str, api_id: int, api_hash: str, link_or_username: str
) -> dict:
    """
    Resolve a channel from username/link/invite and auto-join if needed.
    Returns {id, title, username}. Raises ValueError with user-friendly message on error.
    """
    async with _make_client(api_id, api_hash, session_string) as client:
        target = _channel_id_from_text(link_or_username)

        if target.startswith("+"):
            try:
                chat = await client.join_chat(target)
                return {
                    "id": chat.id,
                    "title": chat.title,
                    "username": f"@{chat.username}" if chat.username else str(chat.id),
                }
            except UserAlreadyParticipant:
                pass
            except InviteHashExpired:
                raise ValueError("❌ This invite link has expired.")
            except InviteHashInvalid:
                raise ValueError("❌ This invite link is invalid.")
            except Exception as e:
                raise ValueError(f"❌ Could not join via invite link: {e}")

        try:
            chat = await client.get_chat(target)
        except ChannelPrivate:
            raise ValueError("❌ This channel is private. Please provide an invite link.")
        except Exception as e:
            raise ValueError(f"❌ Could not resolve channel: {e}")

        try:
            await client.join_chat(target)
        except UserAlreadyParticipant:
            pass
        except Exception as e:
            raise ValueError(f"❌ Could not join channel: {e}")

        return {
            "id": chat.id,
            "title": chat.title,
            "username": f"@{chat.username}" if chat.username else str(chat.id),
        }


# ─────────────────────────────  Forward engine  ──────────────────────────────

ProgressCallback = Callable[[int, int, int], Awaitable[None]]


async def forward_messages(
    session_string: str,
    api_id: int,
    api_hash: str,
    source: str | int,
    destinations: list[str | int],
    start_id: int,
    end_id: int,
    caption: str | None,
    progress_cb: ProgressCallback | None = None,
    stop_event: asyncio.Event | None = None,
) -> dict:
    """
    Copy messages without "Forwarded" tag using copy_message().
    Returns {forwarded, errors, skipped}.
    """
    forwarded = 0
    errors = 0
    skipped = 0
    total = end_id - start_id + 1

    async with _make_client(api_id, api_hash, session_string) as client:
        for msg_id in range(start_id, end_id + 1):
            if stop_event and stop_event.is_set():
                break
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

            effective_caption = caption if caption is not None else (msg.caption or msg.text or "")

            for dest in destinations:
                try:
                    await client.copy_message(
                        chat_id=dest,
                        from_chat_id=source,
                        message_id=msg_id,
                        caption=effective_caption,
                        parse_mode=None if not effective_caption else "markdown",
                    )
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    try:
                        await client.copy_message(
                            chat_id=dest,
                            from_chat_id=source,
                            message_id=msg_id,
                            caption=effective_caption,
                            parse_mode=None if not effective_caption else "markdown",
                        )
                    except Exception:
                        errors += 1
                except ChatAdminRequired:
                    raise ValueError(f"❌ Account needs posting rights in destination: {dest}")
                except Exception:
                    errors += 1

            forwarded += 1
            if progress_cb and forwarded % config.PROGRESS_INTERVAL == 0:
                await progress_cb(forwarded, total, errors)

            await asyncio.sleep(0.3)

    return {"forwarded": forwarded, "errors": errors, "skipped": skipped}

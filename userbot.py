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
    ChatForwardsRestricted,
)
from pyrogram.types import Chat, Message

import config

# ─────────────────────────────  Client factory  ──────────────────────────────

# Pyrogram needs api_id/api_hash even when a session_string is provided.
# The auth key is embedded in the session string itself, so any valid pair works.
_DEFAULT_API_ID   = 2
_DEFAULT_API_HASH = "36722c72256a24c1225de00eb6a1ca74"

DOWNLOAD_TIMEOUT = 600  # 10 minutes max per file download


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
        no_updates=True,      # crucial — prevents hang on connect
        workers=24,           # more parallel MTProto workers → faster downloads
        sleep_threshold=60,   # wait max 60s for flood-wait before erroring
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


ProgressCallback = Callable[[int, int, int, str], Awaitable[None]]


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
    # Types that cannot be downloaded (must be resent differently)
    NON_DOWNLOADABLE = ("poll", "contact", "location", "venue", "dice")

    async with _make_client(session_string=session_string) as client:
        # ── Warm up peer cache via dialogs ────────────────────────────────────────
        needed_ids: set = {source} | {d for d in destinations}
        found_ids: set = set()
        try:
            async for dialog in client.get_dialogs(limit=200):  # scan top 200 dialogs
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

        def make_prog(action_name):
            async def _prog(current, total_bytes):
                if progress_cb and total_bytes:
                    pct = int(current * 100 / total_bytes)
                    await progress_cb(forwarded, total, errors, f"{action_name} {pct}%")
            return _prog

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

            # Skip service messages (join/leave/pin/unpin) — can never be forwarded
            if msg.service is not None:
                skipped += 1
                continue

            # ── Determine message type ─────────────────────────────────────────
            is_text_only = (
                # Web page previews: Pyrogram puts text in msg.caption (not msg.text).
                # So we treat msg.caption as text-only too when no real media is attached.
                (msg.text is not None or msg.caption is not None) and
                msg.video is None and msg.photo is None and
                msg.audio is None and msg.document is None and
                msg.animation is None and msg.voice is None and
                msg.sticker is None and msg.poll is None and
                msg.contact is None and getattr(msg, 'dice', None) is None and
                msg.video_note is None
            )
            is_media = any(getattr(msg, t, None) is not None for t in MEDIA_TYPES)
            is_non_dl = any(getattr(msg, t, None) is not None for t in NON_DOWNLOADABLE)
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
                    # Custom thumbnail on video/doc: copy_message can't apply it — must reupload
                    if thumbnail_path and can_have_thumbnail:
                        await _restricted_send(dest_id)
                        return
                    try:
                        if override_caption:
                            await client.copy_message(
                                chat_id=dest_id,
                                from_chat_id=source,
                                message_id=msg_id,
                                caption=effective_caption,
                            )
                        else:
                            await client.copy_message(
                                chat_id=dest_id,
                                from_chat_id=source,
                                message_id=msg_id,
                            )
                    except ChatForwardsRestricted:
                        # Restricted channel — use manual re-send fallback
                        await _restricted_send(dest_id)
                    except Exception as _copy_err:
                        # Any other copy failure — try manual fallback then raw forward
                        try:
                            await _restricted_send(dest_id)
                        except Exception as _rsend_err:
                            # Last resort: Telegram forward_messages (shows “Forwarded from” tag)
                            try:
                                await client.forward_messages(
                                    chat_id=dest_id,
                                    from_chat_id=source,
                                    message_ids=msg_id,
                                )
                            except Exception as _fwd_err:
                                # Level 4: nuclear text extraction — send ANYTHING we can find
                                _wp4 = getattr(msg, 'web_page', None)
                                _nuke_text = (
                                    msg.text or msg.caption
                                    or (getattr(_wp4, 'description', None) if _wp4 else None)
                                    or (getattr(_wp4, 'title', None) if _wp4 else None)
                                    or (getattr(_wp4, 'url', None) if _wp4 else None)
                                    or (getattr(_wp4, 'display_url', None) if _wp4 else None)
                                )
                                if _nuke_text:
                                    _nuke_ents = msg.entities or msg.caption_entities or []
                                    await client.send_message(
                                        chat_id=dest_id,
                                        text=str(_nuke_text),
                                        entities=_nuke_ents if _nuke_ents else None,
                                        parse_mode=None,
                                        disable_web_page_preview=False,
                                    )
                                else:
                                    raise Exception(
                                        f"Copy: {_copy_err} | Fallback: {_rsend_err} | Forward: {_fwd_err}"
                                    ) from _fwd_err

                async def _restricted_send(dest_id):
                    """Re-send a message from a restricted channel without copying."""
                    ents = msg.caption_entities if not override_caption else None
                    thumb_kwargs = {}
                    if thumbnail_path and can_have_thumbnail and os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
                        thumb_kwargs = {"thumb": thumbnail_path}

                    if is_text_only:
                        # Text message (or web page preview w/ text in caption)
                        # Use both fields: Pyrogram may put text in either depending on media type
                        raw_text = msg.text or msg.caption or ""
                        raw_entities = msg.entities or msg.caption_entities or []
                        wp = getattr(msg, 'web_page', None)
                        wp_photo = getattr(wp, 'photo', None) if wp else None
                        # If there's a web page thumbnail, send it as photo+caption
                        if wp_photo and raw_text:
                            _wpdl = None
                            try:
                                _wpdl = await asyncio.wait_for(
                                    client.download_media(wp_photo, in_memory=False),
                                    timeout=60,
                                )
                                if _wpdl and os.path.getsize(_wpdl) > 0:
                                    await client.send_photo(
                                        chat_id=dest_id,
                                        photo=_wpdl,
                                        caption=raw_text,
                                        parse_mode=None,
                                        caption_entities=raw_entities if raw_entities else None,
                                    )
                                    return
                            except Exception:
                                pass
                            finally:
                                if _wpdl:
                                    try: os.remove(_wpdl)
                                    except Exception: pass
                        # No web page photo (or download failed) — send as plain text
                        if raw_text:
                            await client.send_message(
                                chat_id=dest_id,
                                text=raw_text,
                                entities=raw_entities if raw_entities else None,
                                parse_mode=None,
                                disable_web_page_preview=False,
                            )
                        else:
                            raise ValueError(f"Message #{msg_id} has no text content")
                    elif msg.poll:
                        poll = msg.poll
                        await client.send_poll(
                            chat_id=dest_id,
                            question=poll.question,
                            options=[opt.text for opt in poll.options],
                            is_anonymous=poll.is_anonymous,
                            allows_multiple_answers=poll.allows_multiple_answers,
                        )
                    elif msg.contact:
                        c = msg.contact
                        await client.send_contact(
                            chat_id=dest_id,
                            phone_number=c.phone_number,
                            first_name=c.first_name,
                            last_name=c.last_name or "",
                        )
                    elif msg.venue:
                        v = msg.venue
                        await client.send_venue(
                            chat_id=dest_id,
                            latitude=v.location.latitude,
                            longitude=v.location.longitude,
                            title=v.title,
                            address=v.address,
                        )
                    elif msg.location:
                        await client.send_location(
                            chat_id=dest_id,
                            latitude=msg.location.latitude,
                            longitude=msg.location.longitude,
                        )
                    elif getattr(msg, 'dice', None):
                        await client.send_dice(
                            chat_id=dest_id,
                            emoji=msg.dice.emoji,
                        )
                    elif is_media or msg.video_note:
                        # Must download and re-upload
                        dl_path = None
                        try:
                            dl_path = await asyncio.wait_for(
                                client.download_media(msg, in_memory=False, progress=make_prog("Downloading")),
                                timeout=DOWNLOAD_TIMEOUT,
                            )
                            if not dl_path:
                                raise ValueError("Download returned no file")
                            if os.path.getsize(dl_path) == 0:
                                raise ValueError("Downloaded file is 0 bytes — likely a Telegram timeout")

                            # Upload helper: try with entities, retry without on failure
                            async def _do_upload(cap_ents):
                                if msg.video:
                                    await client.send_video(dest_id, video=dl_path, caption=effective_caption, parse_mode=None, caption_entities=cap_ents, progress=make_prog("Uploading"), **thumb_kwargs)
                                elif msg.document:
                                    await client.send_document(dest_id, document=dl_path, caption=effective_caption, parse_mode=None, caption_entities=cap_ents, progress=make_prog("Uploading"), **thumb_kwargs)
                                elif msg.photo:
                                    await client.send_photo(dest_id, photo=dl_path, caption=effective_caption, parse_mode=None, caption_entities=cap_ents, progress=make_prog("Uploading"))
                                elif msg.audio:
                                    await client.send_audio(dest_id, audio=dl_path, caption=effective_caption, parse_mode=None, caption_entities=cap_ents, progress=make_prog("Uploading"))
                                elif msg.voice:
                                    await client.send_voice(dest_id, voice=dl_path, caption=effective_caption, parse_mode=None, caption_entities=cap_ents, progress=make_prog("Uploading"))
                                elif msg.video_note:
                                    await client.send_video_note(dest_id, video_note=dl_path, progress=make_prog("Uploading"))
                                elif msg.animation:
                                    await client.send_animation(dest_id, animation=dl_path, caption=effective_caption, parse_mode=None, caption_entities=cap_ents, progress=make_prog("Uploading"))
                                elif msg.sticker:
                                    await client.send_sticker(dest_id, sticker=dl_path, progress=make_prog("Uploading"))
                                else:
                                    await client.send_document(dest_id, document=dl_path, caption=effective_caption, parse_mode=None, caption_entities=cap_ents, progress=make_prog("Uploading"), **thumb_kwargs)

                            try:
                                await _do_upload(ents)
                            except Exception:
                                if ents:  # retry without entities (fixes TextUrl/Spoiler issues)
                                    await _do_upload(None)
                                else:
                                    raise
                        except asyncio.TimeoutError:
                            raise ValueError(f"Download timed out after {DOWNLOAD_TIMEOUT // 60} min — file may be too large")
                        finally:
                            if dl_path:
                                try:
                                    os.remove(dl_path)
                                except Exception:
                                    pass
                    elif getattr(msg, 'web_page', None) is not None:
                        # ── Web page preview message ──────────────────────────────
                        wp = msg.web_page
                        # Collect text from all possible sources
                        text_c = (
                            msg.caption or msg.text
                            or getattr(wp, 'description', None)
                            or getattr(wp, 'title', None)
                            or getattr(wp, 'url', None)
                            or getattr(wp, 'display_url', None)
                            or ""
                        )
                        ents_c = msg.caption_entities or msg.entities or []
                        wp_photo = getattr(wp, 'photo', None)
                        # Try to download and forward the preview thumbnail
                        _wpdl = None
                        try:
                            if wp_photo:
                                _wpdl = await asyncio.wait_for(
                                    client.download_media(wp_photo, in_memory=False),
                                    timeout=60,
                                )
                        except Exception:
                            pass
                        if _wpdl and os.path.getsize(_wpdl) > 0:
                            try:
                                await client.send_photo(
                                    chat_id=dest_id,
                                    photo=_wpdl,
                                    caption=text_c or None,
                                    parse_mode=None,
                                    caption_entities=ents_c if ents_c else None,
                                )
                                return
                            except Exception:
                                pass
                            finally:
                                try: os.remove(_wpdl)
                                except Exception: pass
                        # Photo unavailable — send text only
                        if text_c:
                            await client.send_message(
                                chat_id=dest_id,
                                text=text_c,
                                entities=ents_c if ents_c else None,
                                parse_mode=None,
                                disable_web_page_preview=False,
                            )
                        else:
                            raise ValueError(f"Web page msg #{msg_id} has no text or photo to forward")
                    else:
                        # Unknown type — final catch-all
                        wp = getattr(msg, 'web_page', None)
                        text_content = (
                            msg.text or msg.caption
                            or (getattr(wp, 'description', None) if wp else None)
                            or (getattr(wp, 'url', None) if wp else None)
                            or ""
                        )
                        text_ents = msg.entities or msg.caption_entities or []
                        _dl = None
                        try:
                            _dl = await asyncio.wait_for(
                                client.download_media(msg, in_memory=False),
                                timeout=DOWNLOAD_TIMEOUT,
                            )
                            if _dl and os.path.getsize(_dl) > 0:
                                await client.send_document(
                                    chat_id=dest_id,
                                    document=_dl,
                                    caption=text_content or None,
                                    parse_mode=None,
                                    caption_entities=text_ents if text_ents else None,
                                )
                                return
                        except Exception:
                            pass
                        finally:
                            if _dl:
                                try: os.remove(_dl)
                                except Exception: pass
                        if text_content:
                            await client.send_message(
                                chat_id=dest_id,
                                text=text_content,
                                entities=text_ents if text_ents else None,
                                parse_mode=None,
                                disable_web_page_preview=False,
                            )
                        else:
                            # ── Raw MTProto fallback ─────────────────────────
                            # Pyrogram returns None for text/caption/web_page
                            # when WebPage is Empty/Pending. The actual text is
                            # in the raw MTProto message.message field.
                            _raw_text = None
                            try:
                                from pyrogram.raw.types import InputMessageID
                                _peer = await client.resolve_peer(source)
                                if hasattr(_peer, 'channel_id'):
                                    from pyrogram.raw.functions.channels import GetMessages as _CM
                                    _rv = await client.invoke(_CM(channel=_peer, id=[InputMessageID(id=msg_id)]))
                                else:
                                    from pyrogram.raw.functions.messages import GetMessages as _MM
                                    _rv = await client.invoke(_MM(id=[InputMessageID(id=msg_id)]))
                                for _rm in getattr(_rv, 'messages', []):
                                    _t = getattr(_rm, 'message', '') or ''
                                    if _t:
                                        _raw_text = _t
                                        break
                            except Exception:
                                pass
                            if _raw_text:
                                await client.send_message(
                                    chat_id=dest_id,
                                    text=_raw_text,
                                    parse_mode=None,
                                    disable_web_page_preview=False,
                                )
                            else:
                                raise ValueError(
                                    f"msg#{msg_id}: text={msg.text!r} cap={msg.caption!r} "
                                    f"wp={bool(getattr(msg,'web_page',None))} "
                                    f"media={getattr(msg,'media',None)} — "
                                    f"raw MTProto also returned no text"
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

            if progress_cb:
                await progress_cb(forwarded, total, errors, "Copying…")

            await asyncio.sleep(0.05)  # minimal yield; FloodWait handles real rate limiting

    result = {"forwarded": forwarded, "errors": errors, "skipped": skipped}
    if last_error:
        result["last_error"] = last_error  # always report last error
    return result


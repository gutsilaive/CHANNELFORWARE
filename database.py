"""
database.py — Supabase helpers for sessions, tasks, settings
"""
from __future__ import annotations
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY
import datetime, uuid

_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ─────────────────────────────  Sessions  ────────────────────────────────────

def get_session(admin_id: int) -> str | None:
    """Return the stored Pyrogram string session, or None."""
    res = (
        _client.table("sessions")
        .select("session_string")
        .eq("admin_id", admin_id)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]["session_string"]
    return None


def save_session(admin_id: int, phone: str, session_string: str) -> None:
    """Upsert (replace) the session for this admin."""
    _client.table("sessions").upsert(
        {
            "admin_id": admin_id,
            "phone": phone,
            "session_string": session_string,
            "updated_at": datetime.datetime.utcnow().isoformat(),
        },
        on_conflict="admin_id",
    ).execute()


def delete_session(admin_id: int) -> None:
    _client.table("sessions").delete().eq("admin_id", admin_id).execute()


# ─────────────────────────────  API Credentials  ─────────────────────────────

def get_api_credentials(admin_id: int) -> dict | None:
    """Return {api_id, api_hash} for this admin, or None if not saved."""
    res = (
        _client.table("api_credentials")
        .select("api_id, api_hash")
        .eq("admin_id", admin_id)
        .limit(1)
        .execute()
    )
    if res.data:
        return {"api_id": res.data[0]["api_id"], "api_hash": res.data[0]["api_hash"]}
    return None


def save_api_credentials(admin_id: int, api_id: int, api_hash: str) -> None:
    """Upsert API credentials for this admin."""
    _client.table("api_credentials").upsert(
        {
            "admin_id": admin_id,
            "api_id": api_id,
            "api_hash": api_hash,
            "updated_at": datetime.datetime.utcnow().isoformat(),
        },
        on_conflict="admin_id",
    ).execute()


# ─────────────────────────────  Tasks  ───────────────────────────────────────

def create_task(
    admin_id: int,
    source: str,
    destinations: list[str],
    caption: str | None,
    start_id: int,
    end_id: int,
) -> str:
    task_id = str(uuid.uuid4())
    _client.table("tasks").insert(
        {
            "id": task_id,
            "admin_id": admin_id,
            "source": source,
            "destinations": destinations,
            "caption": caption,
            "start_msg_id": start_id,
            "end_msg_id": end_id,
            "total": end_id - start_id + 1,
            "forwarded": 0,
            "status": "pending",
            "created_at": datetime.datetime.utcnow().isoformat(),
        }
    ).execute()
    return task_id


def update_task_progress(task_id: str, forwarded: int, status: str = "running") -> None:
    _client.table("tasks").update(
        {"forwarded": forwarded, "status": status}
    ).eq("id", task_id).execute()


def finish_task(task_id: str, status: str = "done", error: str | None = None) -> None:
    payload: dict = {"status": status, "finished_at": datetime.datetime.utcnow().isoformat()}
    if error:
        payload["error"] = error[:500]
    _client.table("tasks").update(payload).eq("id", task_id).execute()


def get_tasks(admin_id: int, limit: int = 10) -> list[dict]:
    res = (
        _client.table("tasks")
        .select("*")
        .eq("admin_id", admin_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []

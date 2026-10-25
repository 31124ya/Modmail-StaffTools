# -*- coding: utf-8 -*-
"""
StaffTools data layer: member counters, thread state, user-facing panels, plugin config.

All data lives in the plugin-specific partition of Modmail's built-in MongoDB
(bot.api.get_plugin_partition) — no extra database setup required.

Document layouts inside the collection
--------------------------------------
1. Member stats:  {"_id": <user_id>, "kind": "member", "name": str,
                  "closes": int, "claims": int, "transfers": int,
                  "replies": int, "first_seen": iso, "last_active": iso}
2. Thread state:  {"_id": "thread:<channel_id>", "kind": "thread",
                  "channel_id": int, "recipient_id": int,
                  "department": str|None, "dept_pending": bool,
                  "claimed_by": {"user_id","name","at"}|None,
                  "co_claimers": {"<uid>": {"by","by_name","at"}},
                  "onhold": {"user_id","name","at"}|None,
                  "escalated": {"user_id","name","at"}|None,
                  "inactivity": {"user_id","name","closes_at","seconds"}|None}
3. User panel:    {"_id": "panel:<key>", "kind": "panel",
                  "format": "embed"|"components", "data": {...}}
4. Plugin config: {"_id": "config", ...constants.DEFAULT_CONFIG}
5. Runtime meta:  {"_id": "meta", "last_digest_at": iso}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .constants import (
    COUNT_FIELDS,
    DEFAULT_CONFIG,
    DOC_CONFIG,
    DOC_MEMBER,
    DOC_META,
    PANEL_ID_PREFIX,
    THREAD_ID_PREFIX,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def thread_doc_id(channel_id) -> str:
    return f"{THREAD_ID_PREFIX}{int(channel_id)}"


def panel_doc_id(key: str) -> str:
    return f"{PANEL_ID_PREFIX}{key}"


class StaffStore:
    """Read/write wrapper around the plugin MongoDB partition."""

    def __init__(self, cog) -> None:
        self.cog = cog
        # get_plugin_partition returns a motor AsyncIOMotorCollection
        self.coll = cog.bot.api.get_plugin_partition(cog)
        self.config: Dict[str, Any] = dict(DEFAULT_CONFIG)

    # ------------------------------------------------------------------
    # Plugin configuration
    # ------------------------------------------------------------------
    async def load_config(self) -> None:
        self.config = dict(DEFAULT_CONFIG)
        doc = await self.coll.find_one({"_id": DOC_CONFIG})
        if not doc:
            return
        for key in DEFAULT_CONFIG:
            if key not in doc:
                continue
            value = doc[key]
            default = DEFAULT_CONFIG[key]
            if isinstance(default, bool):
                self.config[key] = bool(value)
            elif isinstance(default, int) and not isinstance(default, bool):
                try:
                    self.config[key] = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(default, list):
                if isinstance(value, list):
                    self.config[key] = value
            elif isinstance(default, dict):
                if isinstance(value, dict):
                    self.config[key] = value
            else:
                self.config[key] = value
        # escalate_role_id is allowed to be None
        if "escalate_role_id" in doc:
            raw = doc["escalate_role_id"]
            try:
                self.config["escalate_role_id"] = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                self.config["escalate_role_id"] = None

    async def save_config(self) -> None:
        payload = dict(self.config)
        payload["_id"] = DOC_CONFIG
        await self.coll.replace_one({"_id": DOC_CONFIG}, payload, upsert=True)

    # ------------------------------------------------------------------
    # Member counters
    # ------------------------------------------------------------------
    async def bump(self, member, field: str, delta: int = 1) -> None:
        """Increment one of a member's counters by delta and refresh last-active."""
        if field not in COUNT_FIELDS:
            raise ValueError(f"unknown counter field: {field}")
        now = iso_now()
        name = getattr(member, "display_name", None) or str(member)
        await self.coll.update_one(
            {"_id": int(member.id), "kind": DOC_MEMBER},
            {
                "$inc": {field: int(delta)},
                "$set": {"name": name, "last_active": now},
                "$setOnInsert": {"first_seen": now},
            },
            upsert=True,
        )

    async def decrement(self, user_id, name: str, field: str) -> int:
        """Decrement one of a member's counters by 1 (floored at 0).

        Returns the adjusted value, or -1 when the member has no record.
        """
        if field not in COUNT_FIELDS:
            raise ValueError(f"unknown counter field: {field}")
        user_id = int(user_id)
        doc = await self.coll.find_one({"_id": user_id, "kind": DOC_MEMBER})
        if not doc:
            return -1
        new_value = max(0, int(doc.get(field) or 0) - 1)
        await self.coll.update_one(
            {"_id": user_id, "kind": DOC_MEMBER},
            {"$set": {field: new_value, "last_active": iso_now()}},
        )
        return new_value

    @staticmethod
    def normalize_member(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Fill a database doc into the display structure (missing counts = 0)."""
        if not doc:
            return {f: 0 for f in COUNT_FIELDS}
        return {
            "_id": doc.get("_id"),
            "name": doc.get("name") or "Unknown member",
            "first_seen": doc.get("first_seen"),
            "last_active": doc.get("last_active"),
            **{f: int(doc.get(f) or 0) for f in COUNT_FIELDS},
        }

    async def get_member(self, user_id) -> Optional[Dict[str, Any]]:
        return await self.coll.find_one({"_id": int(user_id), "kind": DOC_MEMBER})

    async def all_members(self) -> List[Dict[str, Any]]:
        cursor = self.coll.find({"kind": DOC_MEMBER})
        return await cursor.to_list(length=None)

    async def reset_members(self) -> int:
        """Wipe all member counters (thread state and config are untouched)."""
        result = await self.coll.delete_many({"kind": DOC_MEMBER})
        return result.deleted_count

    # ------------------------------------------------------------------
    # Thread state
    # ------------------------------------------------------------------
    async def get_thread(self, channel_id) -> Optional[Dict[str, Any]]:
        return await self.coll.find_one({"_id": thread_doc_id(channel_id)})

    async def save_thread(self, doc: Dict[str, Any]) -> None:
        doc = dict(doc)
        doc["_id"] = thread_doc_id(doc["channel_id"])
        doc["kind"] = "thread"
        await self.coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    async def update_thread(self, channel_id, update: Dict[str, Any]) -> None:
        await self.coll.update_one(
            {"_id": thread_doc_id(channel_id)}, {"$set": update}, upsert=True
        )

    async def delete_thread(self, channel_id) -> None:
        await self.coll.delete_one({"_id": thread_doc_id(channel_id)})

    async def all_threads(self) -> List[Dict[str, Any]]:
        cursor = self.coll.find({"kind": "thread"})
        return await cursor.to_list(length=None)

    # ------------------------------------------------------------------
    # User-facing panels
    # ------------------------------------------------------------------
    async def get_panel(self, key: str) -> Optional[Dict[str, Any]]:
        return await self.coll.find_one({"_id": panel_doc_id(key)})

    async def set_panel(self, key: str, fmt: str, data: Any) -> None:
        await self.coll.replace_one(
            {"_id": panel_doc_id(key)},
            {"_id": panel_doc_id(key), "kind": "panel", "format": fmt, "data": data},
            upsert=True,
        )

    async def delete_panel(self, key: str) -> None:
        await self.coll.delete_one({"_id": panel_doc_id(key)})

    async def all_panels(self) -> List[Dict[str, Any]]:
        cursor = self.coll.find({"kind": "panel"})
        return await cursor.to_list(length=None)

    # ------------------------------------------------------------------
    # Runtime metadata
    # ------------------------------------------------------------------
    async def get_meta(self) -> Dict[str, Any]:
        doc = await self.coll.find_one({"_id": DOC_META})
        return doc or {}

    async def set_meta(self, field: str, value: Any) -> None:
        await self.coll.update_one(
            {"_id": DOC_META}, {"$set": {field: value}}, upsert=True
        )

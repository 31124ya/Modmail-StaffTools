# -*- coding: utf-8 -*-
"""
StaffTools — Modmail staff tooling & statistics plugin (main Cog).

Feature overview
================
1. ?claim / ?unclaim — claim the current ticket (DMs the user a custom panel),
   claims are counted automatically
2. ?addclaim / ?removeclaim — add / remove co-claimants (claim counts adjusted
   precisely)
3. ?closerequest — send the user a close-confirmation panel; when the user
   clicks it, the close is counted toward the requesting staff member
4. ?inactivity [duration] — no-reply auto-close timer (24h by default); the
   automatic close counts toward the scheduler's closes; a user reply cancels
   the timer (configurable). Subcommands: cancel / report / set / roles
5. ?escalate — escalate a ticket; every N hours the configured role receives a
   DM digest of pending escalated tickets (nothing is sent when there are none)
6. ?onhold / ?resume — hold / resume a ticket
7. ?transfer <department> — move the ticket to another department (users can be
   forced to pick a department when opening a ticket)
8. ?stats — team leaderboard (claims / closes / transfers) and personal cards
9. ?stafftools — panel JSON customization (embed or Components V2) and config

Storage: everything uses Modmail's built-in MongoDB plugin partition
(bot.api.get_plugin_partition).

Install: ?plugin add <your-github-username>/modmail-stafftools/stafftools
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands, tasks

from core import checks
from core.models import PermissionLevel, getLogger

from .constants import (
    CID_CLOSE_REQUEST,
    CID_DEPT_SELECT,
    COUNT_FIELDS,
    CUSTOM_ID_PREFIX,
    FIELD_CLAIMS,
    FIELD_CLOSES,
    FIELD_REPLIES,
    FIELD_TRANSFERS,
    FUNCTIONAL_PANEL_KEYS,
    INACTIVITY_REPORT_MAX_ROWS,
    LEADERBOARD_FIELDS,
    LEADERBOARD_SIZE,
    MAX_ESCALATE_INTERVAL,
    MAX_INACTIVITY_SECONDS,
    MEDALS,
    MIN_ESCALATE_INTERVAL,
    MIN_INACTIVITY_SECONDS,
    PANEL_KEYS,
    STAFF_PERMISSION_LEVELS,
    VERSION,
)
from .panels import _contains_custom_id, detect_format, render_panel, resolve_panel_source
from .store import StaffStore, iso_now, parse_iso, utcnow

logger = getLogger(__name__)

DURATION_RE = re.compile(r"^(\d+)\s*([dhms]?)$", re.IGNORECASE)
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Top-level command names used to detect foreign conflicts at setup time
TOP_LEVEL_COMMANDS = (
    "claim",
    "unclaim",
    "addclaim",
    "removeclaim",
    "closerequest",
    "inactivity",
    "escalate",
    "onhold",
    "resume",
    "transfer",
    "stats",
    "stafftools",
)


# ---------------------------------------------------------------------------
# Pure helpers (kept simple for unit testing)
# ---------------------------------------------------------------------------
def relative_time(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Format a point in time as a natural-language relative time string."""
    if dt is None:
        return "never"
    if now is None:
        now = utcnow()
    seconds = (now - dt).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"

    def _plural(n: int, unit: str) -> str:
        return f"{n} {unit}" if n == 1 else f"{n} {unit}s"

    if seconds < 3600:
        return _plural(int(seconds // 60), "minute") + " ago"
    if seconds < 86400:
        return _plural(int(seconds // 3600), "hour") + " ago"
    days = int(seconds // 86400)
    if days < 30:
        return _plural(days, "day") + " ago"
    months = days // 30
    if months < 12:
        return _plural(months, "month") + " ago"
    return _plural(days // 365, "year") + " ago"


def format_duration(seconds: int) -> str:
    """Format a number of seconds as a natural-language duration string."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0 seconds"
    if seconds % 86400 == 0:
        n = seconds // 86400
        return "1 day" if n == 1 else f"{n} days"
    if seconds % 3600 == 0:
        n = seconds // 3600
        return "1 hour" if n == 1 else f"{n} hours"
    if seconds % 60 == 0:
        n = seconds // 60
        return "1 minute" if n == 1 else f"{n} minutes"
    return "1 second" if seconds == 1 else f"{seconds} seconds"


def parse_duration(text: Optional[str]) -> Optional[int]:
    """Parse a duration string (90m / 24h / 2d / plain number = hours) into seconds."""
    if not text:
        return None
    match = DURATION_RE.match(str(text).strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = (match.group(2) or "h").lower()
    return amount * DURATION_UNITS[unit]


def clamp_inactivity(seconds: int) -> int:
    return max(MIN_INACTIVITY_SECONDS, min(MAX_INACTIVITY_SECONDS, int(seconds)))


def rank_members(docs: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Leaderboard sort: closes -> claims -> transfers, top `limit` entries."""
    ordered = sorted(
        docs,
        key=lambda d: tuple(-int(d.get(f) or 0) for f in LEADERBOARD_FIELDS)
        + (str(d.get("name") or ""),),
    )
    return ordered[:limit]


def select_due_inactivity(docs: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """Filter thread docs whose inactivity timer is due."""
    due: List[Dict[str, Any]] = []
    for doc in docs:
        info = doc.get("inactivity") or {}
        closes_at = parse_iso(info.get("closes_at"))
        if closes_at is not None and closes_at <= now:
            due.append(doc)
    return due


def digest_due(last_iso: Optional[str], interval_hours: int, now: datetime) -> bool:
    """Whether the escalation digest is due (missing last timestamp = not due)."""
    last = parse_iso(last_iso)
    if last is None:
        return False
    return (now - last).total_seconds() >= max(1, int(interval_hours)) * 3600


def strip_codeblock(text: Optional[str]) -> str:
    """Strip a wrapping ``` code fence from JSON input."""
    if not text:
        return ""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_bool(text: Optional[str]) -> Optional[bool]:
    """Parse a boolean config value; None when unrecognized."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if t in ("1", "true", "yes", "y", "on"):
        return True
    if t in ("0", "false", "no", "n", "off"):
        return False
    return None


def detect_panel_format(payload: Any) -> Optional[str]:
    """Detect the panel JSON format (embed / components); None when unknown."""
    return detect_format(payload)


def collect_staff_ids(bot, staff_role_ids: List[int]) -> set:
    """Collect the member IDs tracked by the activity report.

    Explicitly configured roles take priority; otherwise roles bound to the
    staff Modmail permission levels are used.
    """
    guild = getattr(bot, "modmail_guild", None) or getattr(bot, "guild", None)
    ids: set = set()
    if guild is None:
        return ids

    role_ids: List[int] = list(staff_role_ids or [])
    if not role_ids:
        level_permissions = {}
        try:
            level_permissions = bot.config.get("level_permissions") or {}
        except AttributeError:
            level_permissions = {}
        for level_name in STAFF_PERMISSION_LEVELS:
            for raw in level_permissions.get(level_name, []) or []:
                try:
                    role_ids.append(int(raw))
                except (TypeError, ValueError):
                    continue

    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is not None:
            ids.update(m.id for m in role.members if not m.bot)
    return ids


def build_inactivity_rows(
    staff_ids: set,
    records: Dict[int, Dict[str, Any]],
    resolver,
    threshold_days: int,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Build activity report rows (longest-idle members first)."""
    if now is None:
        now = utcnow()
    rows: List[Dict[str, Any]] = []
    for user_id in staff_ids:
        member = resolver(user_id)
        doc = records.get(user_id) or {}
        last_active = parse_iso(doc.get("last_active"))
        idle_days = None
        if last_active is not None:
            idle_days = (now - last_active).total_seconds() / 86400
        display = getattr(member, "display_name", None) or doc.get("name") or f"User {user_id}"
        rows.append(
            {
                "user_id": user_id,
                "display": display,
                "member": member,
                "last_active": last_active,
                "idle_days": idle_days,
                "inactive": idle_days is None or idle_days > threshold_days,
                "counts": {f: int(doc.get(f) or 0) for f in COUNT_FIELDS},
            }
        )
    rows.sort(key=lambda r: (r["idle_days"] is not None, -(r["idle_days"] or 0)))
    return rows


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------
class StaffTools(commands.Cog):
    """StaffTools — claim / close request / auto-close / escalate / hold / transfer / stats."""

    def __init__(self, bot):
        self.bot = bot
        self.store = StaffStore(self)

    async def cog_load(self):
        await self.store.load_config()
        meta = await self.store.get_meta()
        if not meta.get("last_digest_at"):
            await self.store.set_meta("last_digest_at", iso_now())
        self.inactivity_ticker.start()
        self.escalate_digest.start()
        logger.info("StaffTools v%s loaded.", VERSION)

    async def cog_unload(self):
        for loop in (self.inactivity_ticker, self.escalate_digest):
            try:
                loop.cancel()
            except Exception:
                pass
        logger.info("StaffTools unloaded.")

    # ------------------------------------------------------------------
    # Basic helpers
    # ------------------------------------------------------------------
    @property
    def _guild(self):
        return getattr(self.bot, "modmail_guild", None) or getattr(self.bot, "guild", None)

    async def ensure_thread_doc(self, thread) -> Dict[str, Any]:
        """Fetch (or initialize) the thread state document."""
        doc = await self.store.get_thread(thread.id)
        if doc is None:
            doc = {
                "channel_id": int(thread.id),
                "recipient_id": int(thread.recipient.id) if thread.recipient else 0,
                "department": None,
                "dept_pending": False,
                "claimed_by": None,
                "co_claimers": {},
                "onhold": None,
                "escalated": None,
                "inactivity": None,
            }
            await self.store.save_thread(doc)
        return doc

    def build_placeholders(
        self,
        thread=None,
        staff=None,
        department: Optional[str] = None,
        duration: Optional[str] = None,
    ) -> Dict[str, str]:
        """Build the placeholder mapping used by panel templates."""
        recipient = getattr(thread, "recipient", None)
        guild = self._guild
        staff_name = getattr(staff, "display_name", None) or (str(staff) if staff else "")
        user_name = getattr(recipient, "display_name", None) or (str(recipient) if recipient else "")
        return {
            "{user}": f"<@{recipient.id}>" if recipient else "",
            "{user_name}": user_name,
            "{staff}": staff_name,
            "{staff_mention}": getattr(staff, "mention", None) or (f"<@{staff.id}>" if staff else ""),
            "{department}": department or "",
            "{duration}": duration or "",
            "{guild}": getattr(guild, "name", "") or "",
            "{channel}": f"<#{thread.id}>" if thread else "",
        }

    def _functional_item_builder(self, kind: str, channel_id, requester_id=None):
        """Factory for functional interactive components (button / department select)."""
        if kind == "close":
            custom_id = f"{CUSTOM_ID_PREFIX}{CID_CLOSE_REQUEST}:{int(channel_id)}:{int(requester_id or 0)}"
            prefix = f"{CUSTOM_ID_PREFIX}{CID_CLOSE_REQUEST}:"

            def build():
                return discord.ui.Button(
                    style=discord.ButtonStyle.danger,
                    label="Close Ticket",
                    emoji="🔒",
                    custom_id=custom_id,
                )

        else:
            custom_id = f"{CUSTOM_ID_PREFIX}{CID_DEPT_SELECT}:{int(channel_id)}"
            prefix = f"{CUSTOM_ID_PREFIX}{CID_DEPT_SELECT}:"
            departments = list((self.store.config.get("departments") or {}).items())[:25]

            def build():
                return discord.ui.Select(
                    custom_id=custom_id,
                    placeholder="Select a department…",
                    options=[discord.SelectOption(label=name, value=name) for name, _ in departments],
                )

        return build, prefix

    def _attach_functional(self, rendered: Dict[str, Any], kind: str, channel_id, requester_id=None) -> None:
        """Attach the functional component to a rendered result
        (a View for embed format, an extra ActionRow for components format)."""
        builder, prefix = self._functional_item_builder(kind, channel_id, requester_id)
        if _contains_custom_id(rendered.get("raw"), prefix):
            # The custom JSON already includes this plugin's functional component
            return
        if rendered.get("layout") is not None:
            rendered["layout"].add_item(discord.ui.ActionRow(builder()))
        else:
            view = rendered.get("view")
            if view is None:
                view = discord.ui.View(timeout=None)
            view.add_item(builder())
            rendered["view"] = view

    async def _send_panel(
        self,
        dest,
        key: str,
        mapping: Dict[str, str],
        *,
        thread=None,
        requester_id=None,
        attach: Optional[str] = None,
    ) -> bool:
        """Render and send a panel; returns True on success."""
        try:
            fmt, data = resolve_panel_source(await self.store.get_panel(key), key)
            rendered = render_panel(fmt, data, mapping, self.bot)
            if attach and thread is not None and key in FUNCTIONAL_PANEL_KEYS:
                self._attach_functional(rendered, attach, thread.id, requester_id)
            if rendered["format"] == "components":
                await dest.send(view=rendered["layout"])
            else:
                kwargs: Dict[str, Any] = {}
                if rendered["content"]:
                    kwargs["content"] = rendered["content"]
                if rendered["embeds"]:
                    kwargs["embeds"] = rendered["embeds"]
                if rendered.get("view"):
                    kwargs["view"] = rendered["view"]
                await dest.send(**kwargs)
            return True
        except Exception:
            logger.exception("Failed to send panel %s", key)
            return False

    async def _note_dm_failed(self, thread, what: str) -> None:
        try:
            embed = discord.Embed(
                title="⚠️ Could not DM the user",
                description=(
                    f"{what} could not be delivered (the user may have DMs disabled). "
                    "Please inform the user directly in the ticket channel."
                ),
                color=self.bot.error_color,
            )
            await thread.channel.send(embed=embed)
        except Exception:
            logger.debug("Sending the DM-failure notice failed too (channel=%s)", getattr(thread, "id", "?"))

    async def _safe_respond(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except discord.InteractionResponded:
            try:
                await interaction.followup.send(content, ephemeral=True)
            except Exception:
                pass
        except Exception:
            logger.debug("Interaction response failed: %s", content)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        """A staff member closed the ticket -> closes +1 (user self-closes and bot
        closes excluded), and the thread state is cleaned up.

        Closes triggered by this plugin's closerequest (closer = requester) and by
        the inactivity timer (closer = scheduler) pass through here too, so they
        are naturally attributed to the right staff member.
        """
        try:
            if closer is not None and not getattr(closer, "bot", False):
                recipient = getattr(thread, "recipient", None)
                if recipient is None or closer.id != recipient.id:
                    await self.store.bump(closer, FIELD_CLOSES)
        except Exception:
            logger.exception("Error while counting a ticket close")
        finally:
            try:
                cid = getattr(thread, "id", None)
                if cid is not None:
                    await self.store.delete_thread(cid)
            except Exception:
                logger.exception("Error while cleaning up thread state")

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        """Count staff replies; when the user replies, cancel the inactivity
        auto-close timer according to configuration."""
        try:
            if from_mod:
                author = getattr(message, "author", None)
                if author is None or getattr(author, "bot", False):
                    return
                recipient = getattr(thread, "recipient", None)
                if recipient is not None and author.id == recipient.id:
                    return
                await self.store.bump(author, FIELD_REPLIES)
                return
            # User reply -> cancel the auto-close timer
            if not self.store.config.get("cancel_on_reply", True):
                return
            doc = await self.store.get_thread(thread.id)
            if not doc or not doc.get("inactivity"):
                return
            info = doc["inactivity"]
            await self.store.update_thread(thread.id, {"inactivity": None})
            embed = discord.Embed(
                title="ℹ️ Auto-close cancelled",
                description=(
                    f"The user replied to this ticket, so the auto-close scheduled by "
                    f"{info.get('name') or 'staff'} has been cancelled."
                ),
                color=self.bot.main_color,
            )
            await thread.channel.send(embed=embed)
        except Exception:
            logger.exception("Error while handling a thread reply event")

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """New ticket: initialize the state document and DM the user a department
        select menu when configured."""
        try:
            departments = self.store.config.get("departments") or {}
            require = self.store.config.get("require_departments", True)
            if not departments or not require or thread.recipient is None:
                return
            doc = await self.ensure_thread_doc(thread)
            if doc.get("department") or doc.get("dept_pending"):
                return
            doc["dept_pending"] = True
            await self.store.save_thread(doc)
            mapping = self.build_placeholders(thread=thread)
            ok = await self._send_panel(
                thread.recipient, "department_select", mapping, thread=thread, attach="dept"
            )
            if not ok:
                doc["dept_pending"] = False
                await self.store.save_thread(doc)
                await self._note_dm_failed(thread, "The department select panel")
        except Exception:
            logger.exception("Error while handling a new ticket")

    # ------------------------------------------------------------------
    # Interaction handling (functional button / department select on panels)
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            if interaction.type is not discord.InteractionType.component:
                return
            data = interaction.data or {}
            custom_id = data.get("custom_id") or ""
            if not isinstance(custom_id, str) or not custom_id.startswith(CUSTOM_ID_PREFIX):
                return
            parts = custom_id.split(":")
            action = parts[1] if len(parts) > 1 else ""
            if action == CID_CLOSE_REQUEST:
                await self._handle_close_request(interaction, parts)
            elif action == CID_DEPT_SELECT:
                await self._handle_dept_select(interaction, parts)
            else:
                await self._safe_respond(interaction, "This component is no longer valid.")
        except discord.InteractionResponded:
            pass
        except Exception:
            logger.exception("Error while handling a panel interaction")

    async def _handle_close_request(self, interaction: discord.Interaction, parts: List[str]):
        if len(parts) < 4:
            await self._safe_respond(interaction, "This button is no longer valid.")
            return
        try:
            channel_id = int(parts[2])
            requester_id = int(parts[3])
        except ValueError:
            await self._safe_respond(interaction, "This button is no longer valid.")
            return
        thread = self.bot.threads.cache.get(channel_id)
        if thread is None:
            await self._safe_respond(interaction, "This ticket is closed or no longer exists.")
            return
        recipient = getattr(thread, "recipient", None)
        if recipient is not None and interaction.user.id != recipient.id:
            await self._safe_respond(interaction, "Only the ticket creator can confirm the close.")
            return
        await self._safe_respond(interaction, "✅ Got it — closing your ticket…")
        guild = self._guild
        closer = guild.get_member(requester_id) if guild is not None else None
        if closer is None or getattr(closer, "bot", False):
            # The requesting staff member has left the guild: still close, but the
            # close cannot be attributed to them
            closer = self.bot.user
        try:
            await thread.close(closer=closer, silent=False, delete_channel=True)
        except Exception:
            logger.exception("Failed to handle the close request (channel=%s)", channel_id)

    async def _handle_dept_select(self, interaction: discord.Interaction, parts: List[str]):
        if len(parts) < 3:
            await self._safe_respond(interaction, "This select menu is no longer valid.")
            return
        try:
            channel_id = int(parts[2])
        except ValueError:
            await self._safe_respond(interaction, "This select menu is no longer valid.")
            return
        values = (interaction.data or {}).get("values") or []
        if not values:
            await self._safe_respond(interaction, "Please pick a department first.")
            return
        dept = str(values[0])
        departments = self.store.config.get("departments") or {}
        if dept not in departments:
            await self._safe_respond(interaction, "This department option is no longer valid, please contact staff.")
            return
        thread = self.bot.threads.cache.get(channel_id)
        if thread is None:
            await self._safe_respond(interaction, "This ticket is closed or no longer exists.")
            return
        recipient = getattr(thread, "recipient", None)
        if recipient is not None and interaction.user.id != recipient.id:
            await self._safe_respond(interaction, "Only the ticket creator can choose the department.")
            return
        guild = self._guild
        try:
            category = guild.get_channel(int(departments[dept])) if guild is not None else None
        except (TypeError, ValueError):
            category = None
        if not isinstance(category, discord.CategoryChannel):
            await self._safe_respond(interaction, "The department category is misconfigured, please contact an administrator.")
            return
        try:
            await thread.channel.edit(category=category)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to move the ticket channel to the department category: %s", exc)
            await self._safe_respond(interaction, "Something went wrong while moving the ticket, please contact staff.")
            return
        doc = await self.ensure_thread_doc(thread)
        doc["department"] = dept
        doc["dept_pending"] = False
        await self.store.save_thread(doc)
        await self._safe_respond(interaction, f"✅ You selected the **{dept}** department — your ticket has been moved.")
        try:
            embed = discord.Embed(
                title="🗂 Ticket Department",
                description=f"The user selected the **{dept}** department.",
                color=self.bot.main_color,
            )
            await thread.channel.send(embed=embed)
        except Exception:
            logger.debug("Failed to send the department notice to the channel (channel=%s)", channel_id)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    @tasks.loop(seconds=30)
    async def inactivity_ticker(self):
        """Scan for due inactivity timers and auto-close (counted toward the scheduler)."""
        try:
            if not self.bot.is_ready():
                return
            now = utcnow()
            due = select_due_inactivity(await self.store.all_threads(), now)
            for doc in due:
                channel_id = doc.get("channel_id")
                info = doc.get("inactivity") or {}
                thread = self.bot.threads.cache.get(channel_id)
                if thread is None:
                    # Channel no longer exists (e.g. closed while offline) -> clean up
                    await self.store.delete_thread(channel_id)
                    continue
                # Clear the timer first to prevent double closes, then close
                await self.store.update_thread(channel_id, {"inactivity": None})
                guild = self._guild
                closer = guild.get_member(info.get("user_id")) if guild is not None else None
                if closer is None:
                    closer = self.bot.user
                try:
                    await thread.close(closer=closer, silent=False, delete_channel=True)
                except Exception:
                    logger.exception("Inactivity auto-close failed (channel=%s)", channel_id)
        except Exception:
            logger.exception("Error inside the inactivity ticker loop")

    @tasks.loop(seconds=60)
    async def escalate_digest(self):
        """Every N hours, DM the escalation role a digest of pending escalated
        tickets (nothing is sent when there are none)."""
        try:
            if not self.bot.is_ready():
                return
            role_id = self.store.config.get("escalate_role_id")
            if not role_id:
                return
            interval_hours = int(self.store.config.get("escalate_interval_hours") or 24)
            now = utcnow()
            meta = await self.store.get_meta()
            if not digest_due(meta.get("last_digest_at"), interval_hours, now):
                return
            # Advance the timestamp first so a send failure does not cause a
            # retry storm every minute
            await self.store.set_meta("last_digest_at", now.isoformat())
            guild = self._guild
            role = guild.get_role(int(role_id)) if guild is not None else None
            if role is None:
                logger.warning("escalate_role_id %s is invalid, skipping the escalation digest", role_id)
                return
            cache = self.bot.threads.cache
            open_docs = [
                d
                for d in await self.store.all_threads()
                if d.get("escalated") and cache.get(d.get("channel_id")) is not None
            ]
            if not open_docs:
                return
            lines = []
            for d in open_docs[:25]:
                info = d.get("escalated") or {}
                recipient = f"<@{d.get('recipient_id')}>" if d.get("recipient_id") else "unknown user"
                by = info.get("name") or f"<@{info.get('user_id')}>"
                when = relative_time(parse_iso(info.get("at")), now)
                dept = d.get("department") or "Uncategorized"
                lines.append(
                    f"• <#{d.get('channel_id')}> — {recipient} · department: {dept} · escalated by {by} {when}"
                )
            embed = discord.Embed(
                title=f"📣 Escalated tickets awaiting review ({len(open_docs)})",
                description="\n".join(lines),
                color=self.bot.main_color,
            )
            embed.set_footer(text="These tickets were flagged with ?escalate and are waiting for senior staff review")
            for member in role.members:
                if member.bot:
                    continue
                try:
                    await member.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("Could not DM the escalation digest to %s", member)
        except Exception:
            logger.exception("Error inside the escalation digest loop")

    # ------------------------------------------------------------------
    # Claim commands
    # ------------------------------------------------------------------
    @commands.command(name="claim", help="Claim the current ticket (sends a custom panel to the user and counts toward your claims).")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def claim(self, ctx: commands.Context):
        thread = ctx.thread
        doc = await self.ensure_thread_doc(thread)
        claimed = doc.get("claimed_by")
        if claimed:
            if claimed.get("user_id") == ctx.author.id:
                desc = "You have already claimed this ticket."
            else:
                claimed_at = parse_iso(claimed.get("at"))
                desc = f"This ticket was already claimed by <@{claimed.get('user_id')}>"
                if claimed_at is not None:
                    desc += f" ({relative_time(claimed_at)})"
                desc += ". To co-work on it, use `?addclaim @member`."
            embed = discord.Embed(title="❌ Cannot claim", description=desc, color=self.bot.error_color)
            await ctx.send(embed=embed)
            return
        if str(ctx.author.id) in (doc.get("co_claimers") or {}):
            embed = discord.Embed(
                title="❌ Cannot claim",
                description="You are already a co-claimant on this ticket.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return

        escalation_cleared = False
        if doc.get("escalated") and self.store.config.get("clear_escalation_on_claim", True):
            doc["escalated"] = None
            escalation_cleared = True

        doc["claimed_by"] = {
            "user_id": ctx.author.id,
            "name": ctx.author.display_name,
            "at": iso_now(),
        }
        await self.store.save_thread(doc)
        await self.store.bump(ctx.author, FIELD_CLAIMS)

        desc = (
            f"{ctx.author.mention} claimed this ticket and will handle it from here.\n"
            "Co-work: `?addclaim @member` · Release: `?unclaim`"
        )
        if escalation_cleared:
            desc += "\n(escalation flag cleared)"
        embed = discord.Embed(title="✅ Ticket claimed", description=desc, color=self.bot.main_color)
        await ctx.send(embed=embed)

        mapping = self.build_placeholders(thread=thread, staff=ctx.author)
        if not await self._send_panel(thread.recipient, "claim", mapping, thread=thread):
            await self._note_dm_failed(thread, "The claim notification")

    @commands.command(name="unclaim", help="Release your claim on the current ticket (only the claimant can do this; stats are not rolled back).")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unclaim(self, ctx: commands.Context):
        doc = await self.ensure_thread_doc(ctx.thread)
        claimed = doc.get("claimed_by")
        if not claimed:
            embed = discord.Embed(
                title="❌ Not claimed",
                description="This ticket is not claimed.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        if claimed.get("user_id") != ctx.author.id:
            embed = discord.Embed(
                title="❌ Not allowed",
                description=f"This ticket is claimed by <@{claimed.get('user_id')}>; only the claimant can release it.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        doc["claimed_by"] = None
        await self.store.save_thread(doc)
        embed = discord.Embed(
            title="🔓 Claim released",
            description="The claim has been released — other staff can now take over with `?claim`.\n(claim statistics are not rolled back)",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @commands.command(name="addclaim", help="Add another staff member as a co-claimant (their claims count +1). Usage: ?addclaim @member")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def addclaim(self, ctx: commands.Context, member: discord.Member):
        doc = await self.ensure_thread_doc(ctx.thread)
        claimed = doc.get("claimed_by")
        if not claimed:
            embed = discord.Embed(
                title="❌ No claim yet",
                description="Claim the ticket with `?claim` first, then add co-claimants.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        if member.bot:
            embed = discord.Embed(
                title="❌ Cannot add", description="Bots cannot be added as co-claimants.", color=self.bot.error_color
            )
            await ctx.send(embed=embed)
            return
        if member.id == claimed.get("user_id"):
            embed = discord.Embed(
                title="❌ Cannot add", description="That member is already the primary claimant of this ticket.", color=self.bot.error_color
            )
            await ctx.send(embed=embed)
            return
        co = doc.get("co_claimers") or {}
        if str(member.id) in co:
            embed = discord.Embed(
                title="❌ Cannot add", description=f"{member.mention} is already in the co-claimant list.", color=self.bot.error_color
            )
            await ctx.send(embed=embed)
            return
        co[str(member.id)] = {
            "by": ctx.author.id,
            "by_name": ctx.author.display_name,
            "at": iso_now(),
        }
        doc["co_claimers"] = co
        await self.store.save_thread(doc)
        await self.store.bump(member, FIELD_CLAIMS)
        embed = discord.Embed(
            title="✅ Co-claimant added",
            description=f"{member.mention} joined this ticket as a co-claimant (their claims count +1).\n"
            f"Remove: `?removeclaim {member.mention}`",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @commands.command(name="removeclaim", help="Remove a co-claimant you added (their claims count -1). Usage: ?removeclaim @member")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def removeclaim(self, ctx: commands.Context, member: discord.Member):
        doc = await self.ensure_thread_doc(ctx.thread)
        claimed = doc.get("claimed_by") or {}
        if member.id == claimed.get("user_id"):
            embed = discord.Embed(
                title="❌ Cannot remove",
                description="That member is the primary claimant — they must run `?unclaim` themselves.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        co = doc.get("co_claimers") or {}
        entry = co.get(str(member.id))
        if not entry:
            embed = discord.Embed(
                title="❌ Not a co-claimant",
                description=f"{member.mention} is not a co-claimant on this ticket.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        is_admin = bool(
            getattr(ctx.author, "guild_permissions", None)
        ) and (
            ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild
        )
        if entry.get("by") != ctx.author.id and not is_admin:
            embed = discord.Embed(
                title="❌ Not allowed",
                description=f"That member was added by <@{entry.get('by')}>; only the adder or an administrator can remove them.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        co.pop(str(member.id), None)
        doc["co_claimers"] = co
        await self.store.save_thread(doc)
        await self.store.decrement(member.id, member.display_name, FIELD_CLAIMS)
        embed = discord.Embed(
            title="✅ Co-claimant removed",
            description=f"{member.mention} was removed from this ticket (their claims count -1).",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Close request
    # ------------------------------------------------------------------
    @commands.command(
        name="closerequest",
        aliases=("closereq",),
        help="Send a close-confirmation panel to the user; when they confirm, the close counts toward your stats.",
    )
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def closerequest(self, ctx: commands.Context):
        thread = ctx.thread
        await self.ensure_thread_doc(thread)
        mapping = self.build_placeholders(thread=thread, staff=ctx.author)
        ok = await self._send_panel(
            thread.recipient,
            "closerequest",
            mapping,
            thread=thread,
            requester_id=ctx.author.id,
            attach="close",
        )
        if ok:
            embed = discord.Embed(
                title="📨 Close request sent",
                description="A close-confirmation panel was sent to the user. Once they click the button, the ticket will be closed (counted toward your closes).",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
        else:
            await self._note_dm_failed(thread, "The close request panel")

    # ------------------------------------------------------------------
    # Inactivity auto-close
    # ------------------------------------------------------------------
    @commands.group(
        name="inactivity",
        aliases=("inactive",),
        invoke_without_command=True,
        help="Schedule a no-reply auto-close (default 24 hours; accepts 90m/24h/2d). When it fires, the close counts toward the scheduler's stats.",
    )
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def inactivity(self, ctx: commands.Context, *, duration: Optional[str] = None):
        seconds = int(self.store.config.get("inactivity_hours") or 24) * 3600
        if duration:
            parsed = parse_duration(duration)
            if parsed is None:
                embed = discord.Embed(
                    title="❌ Invalid duration",
                    description="Supported formats: `90m` (minutes), `24h` (hours), `2d` (days), or a plain number (hours).\nExample: `?inactivity 48h`",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
            seconds = parsed
        seconds = clamp_inactivity(seconds)
        closes_at = utcnow() + timedelta(seconds=seconds)

        thread = ctx.thread
        doc = await self.ensure_thread_doc(thread)
        doc["inactivity"] = {
            "user_id": ctx.author.id,
            "name": ctx.author.display_name,
            "closes_at": closes_at.isoformat(),
            "seconds": seconds,
        }
        await self.store.save_thread(doc)

        duration_text = format_duration(seconds)
        mapping = self.build_placeholders(
            thread=thread, staff=ctx.author, duration=duration_text
        )
        dm_ok = await self._send_panel(thread.recipient, "inactivity", mapping, thread=thread)

        embed = discord.Embed(
            title="⏳ Auto-close scheduled",
            description=(
                f"If the user does not reply within **{duration_text}**, this ticket will be closed "
                f"automatically (that close counts toward your closes).\n"
                f"Fires: <t:{int(closes_at.timestamp())}:R>\n"
                f"Cancel: `?inactivity cancel`"
            ),
            color=self.bot.main_color,
        )
        if not dm_ok:
            embed.add_field(name="⚠️ Notice", value="The auto-close notice could not be DM'd to the user.", inline=False)
        await ctx.send(embed=embed)

    @inactivity.command(name="cancel", help="Cancel the no-reply auto-close on the current ticket.")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def inactivity_cancel(self, ctx: commands.Context):
        doc = await self.store.get_thread(ctx.thread.id)
        if not doc or not doc.get("inactivity"):
            embed = discord.Embed(
                title="❌ No active auto-close",
                description="No auto-close is scheduled on this ticket. Schedule one: `?inactivity 24h`",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        await self.store.update_thread(ctx.thread.id, {"inactivity": None})
        embed = discord.Embed(
            title="✅ Auto-close cancelled", description="The no-reply auto-close on this ticket has been cancelled.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    @inactivity.command(name="report", help="Show the staff activity report (sorted by last active time).")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def inactivity_report(self, ctx: commands.Context):
        await self._send_inactivity_report(ctx, int(self.store.config.get("report_days") or 7))

    @inactivity.command(name="set", help="Set the activity report threshold (days). Usage: ?inactivity set 7")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def inactivity_set(self, ctx: commands.Context, days: int):
        if not 1 <= days <= 365:
            embed = discord.Embed(
                title="❌ Invalid number of days", description="The threshold must be between 1 and 365 days.", color=self.bot.error_color
            )
            await ctx.send(embed=embed)
            return
        self.store.config["report_days"] = days
        await self.store.save_config()
        embed = discord.Embed(
            title="✅ Threshold updated", description=f"The activity report threshold is now **{days} day(s)**.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    @inactivity.command(name="roles", help="Show or set the staff roles. Usage: ?inactivity roles @role1 @role2")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def inactivity_roles(
        self, ctx: commands.Context, roles: commands.Greedy[discord.Role] = None
    ):
        current: List[int] = list(self.store.config.get("staff_role_ids") or [])
        if not roles:
            if current:
                mentions = ", ".join(f"<@&{rid}>" for rid in current)
                desc = f"Current staff roles: {mentions}"
            else:
                desc = "No dedicated staff roles configured — roles bound to Modmail permission levels (SUPPORTER and above) will be used automatically."
            embed = discord.Embed(
                title="🏷️ Staff Roles", description=desc + "\n\nChange: `?inactivity roles @role1 @role2`",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return
        self.store.config["staff_role_ids"] = [r.id for r in roles]
        await self.store.save_config()
        mentions = ", ".join(r.mention for r in roles)
        embed = discord.Embed(
            title="✅ Staff roles updated",
            description=f"The activity report will track members of: {mentions}",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    async def _send_inactivity_report(self, ctx: commands.Context, threshold_days: int):
        guild = self._guild
        if guild is None:
            embed = discord.Embed(
                title="❌ Guild not found", description="Could not find the Modmail guild, cannot build the report.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        records = {int(d["_id"]): d for d in await self.store.all_members()}
        staff_ids = collect_staff_ids(self.bot, self.store.config.get("staff_role_ids") or [])
        member_ids = set(staff_ids) | {
            uid for uid in records if guild.get_member(uid) is not None
        }
        if not member_ids:
            embed = discord.Embed(
                title="📋 Staff Activity Report",
                description="No staff members found. Configure staff roles first: `?inactivity roles @role`",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return
        rows = build_inactivity_rows(member_ids, records, guild.get_member, threshold_days)
        inactive_rows = [r for r in rows if r["inactive"]]

        lines = []
        for i, row in enumerate(rows):
            if i >= INACTIVITY_REPORT_MAX_ROWS:
                lines.append(f"…and {len(rows)} members in total (truncated)")
                break
            mark = "⚠️" if row["inactive"] else "✅"
            when = relative_time(row["last_active"])
            counts = row["counts"]
            lines.append(
                f"{mark} **{row['display']}** — last active: {when}"
                f" (closed {counts[FIELD_CLOSES]} / claimed {counts[FIELD_CLAIMS]}"
                f" / transferred {counts[FIELD_TRANSFERS]} / replies {counts[FIELD_REPLIES]})"
            )
        embed = discord.Embed(
            title=f"📋 Staff Activity Report (threshold: {threshold_days} days)",
            description="\n".join(lines),
            color=self.bot.main_color,
        )
        summary = (
            f"**{len(rows)}** members tracked · ⚠️ inactive for more than {threshold_days} days: **{len(inactive_rows)}**"
        )
        embed.add_field(name="Summary", value=summary, inline=False)
        embed.set_footer(text="✅ active · ⚠️ inactive / no records · adjust threshold: ?inactivity set <days>")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Escalate / hold / resume / transfer
    # ------------------------------------------------------------------
    @commands.command(
        name="escalate", help="Escalate the ticket to senior staff; every N hours the configured role receives a DM digest of pending tickets."
    )
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def escalate(self, ctx: commands.Context):
        thread = ctx.thread
        doc = await self.ensure_thread_doc(thread)
        existing = doc.get("escalated")
        if existing:
            embed = discord.Embed(
                title="❌ Already escalated",
                description=(
                    f"This ticket was already escalated by <@{existing.get('user_id')}> "
                    f"{relative_time(parse_iso(existing.get('at')))}."
                ),
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        doc["escalated"] = {
            "user_id": ctx.author.id,
            "name": ctx.author.display_name,
            "at": iso_now(),
        }
        await self.store.save_thread(doc)

        role_id = self.store.config.get("escalate_role_id")
        interval_h = int(self.store.config.get("escalate_interval_hours") or 24)
        if role_id:
            note = (
                f"Every **{interval_h}** hours, pending escalated tickets are DM'd to <@&{role_id}>.\n"
                f"(the flag clears automatically when senior staff `?claim` the ticket)"
            )
        else:
            note = "⚠️ No digest role configured (`?stafftools config set escalate_role_id @role`) — no DM digest will be sent for now."
        embed = discord.Embed(
            title="📣 Ticket escalated", description=f"This ticket is now flagged for senior staff review.\n{note}",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

        mapping = self.build_placeholders(thread=thread, staff=ctx.author)
        if not await self._send_panel(thread.recipient, "escalate", mapping, thread=thread):
            await self._note_dm_failed(thread, "The escalation notice")

    @commands.command(name="onhold", aliases=("hold",), help="Put the current ticket on hold.")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def onhold(self, ctx: commands.Context):
        thread = ctx.thread
        doc = await self.ensure_thread_doc(thread)
        if doc.get("onhold"):
            embed = discord.Embed(
                title="❌ Already on hold",
                description=f"This ticket was put on hold {relative_time(parse_iso(doc['onhold'].get('at')))}. Resume: `?resume`",
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        doc["onhold"] = {
            "user_id": ctx.author.id,
            "name": ctx.author.display_name,
            "at": iso_now(),
        }
        await self.store.save_thread(doc)
        embed = discord.Embed(
            title="⏸ Ticket on hold",
            description="This ticket is now on hold. Resume: `?resume`",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)
        mapping = self.build_placeholders(thread=thread, staff=ctx.author)
        if not await self._send_panel(thread.recipient, "onhold", mapping, thread=thread):
            await self._note_dm_failed(thread, "The hold notice")

    @commands.command(name="resume", aliases=("unhold",), help="Resume a ticket that is on hold.")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def resume(self, ctx: commands.Context):
        thread = ctx.thread
        doc = await self.ensure_thread_doc(thread)
        if not doc.get("onhold"):
            embed = discord.Embed(
                title="❌ Not on hold", description="This ticket is not on hold. Put it on hold: `?onhold`", color=self.bot.error_color
            )
            await ctx.send(embed=embed)
            return
        doc["onhold"] = None
        await self.store.save_thread(doc)
        embed = discord.Embed(
            title="▶️ Ticket resumed", description="This ticket has been resumed.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)
        mapping = self.build_placeholders(thread=thread, staff=ctx.author)
        if not await self._send_panel(thread.recipient, "resume", mapping, thread=thread):
            await self._note_dm_failed(thread, "The resume notice")

    @commands.command(name="transfer", help="Move the ticket to another department. Usage: ?transfer <department>")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def transfer(self, ctx: commands.Context, *, department: Optional[str] = None):
        departments = self.store.config.get("departments") or {}
        dept_list = ", ".join(f"**{k}**" for k in departments)
        if not departments:
            embed = discord.Embed(
                title="❌ No departments configured",
                description='Configure departments first: `?stafftools config set departments {"Sales": 111111, ...}`',
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        if not department:
            embed = discord.Embed(
                title="🔀 Transfer ticket",
                description=f"Usage: `?transfer <department>`\nAvailable departments: {dept_list}",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return
        lookup = {str(k).lower(): str(k) for k in departments}
        key = lookup.get(department.strip().lower())
        if key is None:
            embed = discord.Embed(
                title="❌ Unknown department",
                description=f'There is no department named "{department}". Available: {dept_list}',
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        guild = self._guild
        try:
            category = guild.get_channel(int(departments[key])) if guild is not None else None
        except (TypeError, ValueError):
            category = None
        if not isinstance(category, discord.CategoryChannel):
            embed = discord.Embed(
                title="❌ Department category misconfigured",
                description=f'The category for department "{key}" does not exist — an administrator should check the `departments` config.',
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        try:
            await ctx.thread.channel.edit(category=category)
        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ Missing permissions",
                description='The bot lacks the "Manage Channels" permission and cannot move the ticket to the target category.',
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        except discord.HTTPException as exc:
            logger.warning("Failed to move the ticket channel while transferring: %s", exc)
            embed = discord.Embed(
                title="❌ Transfer failed", description="Something went wrong while moving the ticket channel. Please try again later.", color=self.bot.error_color
            )
            await ctx.send(embed=embed)
            return

        doc = await self.ensure_thread_doc(ctx.thread)
        doc["department"] = key
        doc["dept_pending"] = False
        await self.store.save_thread(doc)
        await self.store.bump(ctx.author, FIELD_TRANSFERS)

        embed = discord.Embed(
            title="🔀 Ticket transferred",
            description=f"The ticket was moved to the **{key}** department (your transfers count +1).",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

        mapping = self.build_placeholders(
            thread=ctx.thread, staff=ctx.author, department=key
        )
        if not await self._send_panel(ctx.thread.recipient, "transfer", mapping, thread=ctx.thread):
            await self._note_dm_failed(ctx.thread, "The transfer notice")

    # ------------------------------------------------------------------
    # Statistics commands
    # ------------------------------------------------------------------
    @commands.group(
        name="stats",
        aliases=("staffstats",),
        invoke_without_command=True,
        help="Show the staff statistics leaderboard (closes / claims / transfers).",
    )
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def stats(self, ctx: commands.Context):
        docs = await self.store.all_members()
        if not docs:
            embed = discord.Embed(
                title="📊 Staff Statistics",
                description="No statistics yet. Records appear automatically as staff claim / close / transfer / reply to tickets.",
                color=self.bot.main_color,
            )
            await ctx.send(embed=embed)
            return
        ranked = rank_members(docs, LEADERBOARD_SIZE)
        lines = []
        for i, doc in enumerate(ranked, 1):
            medal = MEDALS[i - 1] if i <= len(MEDALS) else f"`#{i}`"
            name = doc.get("name") or f"User {doc.get('_id')}"
            lines.append(
                f"{medal} **{name}** — closes `{int(doc.get(FIELD_CLOSES) or 0)}`"
                f" · claims `{int(doc.get(FIELD_CLAIMS) or 0)}`"
                f" · transfers `{int(doc.get(FIELD_TRANSFERS) or 0)}`"
            )
        totals = {f: sum(int(d.get(f) or 0) for d in docs) for f in COUNT_FIELDS}
        embed = discord.Embed(
            title="📊 Staff Statistics", description="\n".join(lines), color=self.bot.main_color
        )
        embed.add_field(
            name="Total",
            value=(
                f"🗂 closes {totals[FIELD_CLOSES]} · 🤝 claims {totals[FIELD_CLAIMS]}"
                f" · 🔀 transfers {totals[FIELD_TRANSFERS]} · 💬 replies {totals[FIELD_REPLIES]}"
            ),
            inline=False,
        )
        embed.set_footer(text=f"{len(docs)} members tracked · personal card: ?stats me / ?stats user @member")
        await ctx.send(embed=embed)

    @stats.command(name="me", help="Show your own statistics card.")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def stats_me(self, ctx: commands.Context):
        await self._send_member_card(ctx, ctx.author)

    @stats.command(name="user", help="Show another member's statistics card. Usage: ?stats user @member")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def stats_user(self, ctx: commands.Context, member: discord.Member):
        await self._send_member_card(ctx, member)

    @stats.command(name="reset", help="Wipe all staff statistics. Usage: ?stats reset confirm")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def stats_reset(self, ctx: commands.Context, confirm: str = ""):
        if confirm.lower() != "confirm":
            embed = discord.Embed(
                title="⚠️ Dangerous operation",
                description=(
                    "This will wipe **all** staff closes / claims / transfers / replies statistics and cannot be undone.\n"
                    "To confirm, run: `?stats reset confirm`"
                ),
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        deleted = await self.store.reset_members()
        embed = discord.Embed(
            title="🗑️ Statistics wiped", description=f"Statistics were wiped for {deleted} member(s).", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    async def _send_member_card(self, ctx: commands.Context, member: discord.Member):
        doc = self.store.normalize_member(await self.store.get_member(member.id))
        embed = discord.Embed(title=f"📊 {member.display_name}'s statistics", color=self.bot.main_color)
        if getattr(member, "display_avatar", None) is not None:
            embed.set_thumbnail(url=member.display_avatar.url)
        now = utcnow()
        embed.add_field(name="🗂 Tickets closed", value=str(doc[FIELD_CLOSES]), inline=True)
        embed.add_field(name="🤝 Tickets claimed", value=str(doc[FIELD_CLAIMS]), inline=True)
        embed.add_field(name="🔀 Tickets transferred", value=str(doc[FIELD_TRANSFERS]), inline=True)
        embed.add_field(name="💬 Staff replies", value=str(doc[FIELD_REPLIES]), inline=True)
        embed.add_field(
            name="⏱ Last active", value=relative_time(parse_iso(doc.get("last_active")), now), inline=True
        )
        embed.add_field(
            name="📥 First recorded", value=relative_time(parse_iso(doc.get("first_seen")), now), inline=True
        )
        workload = doc[FIELD_CLOSES] + doc[FIELD_CLAIMS] + doc[FIELD_TRANSFERS] + doc[FIELD_REPLIES]
        embed.add_field(name="📈 Total workload", value=str(workload), inline=True)
        embed.set_footer(text=f"Member ID: {member.id} · Leaderboard: ?stats")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Plugin management: panels & config
    # ------------------------------------------------------------------
    @commands.group(
        name="stafftools",
        aliases=("st",),
        invoke_without_command=True,
        help="StaffTools management: panel (user-facing message JSON) and config (plugin settings).",
    )
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def stafftools(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🧰 StaffTools Management",
            description=(
                "**Panel customization** (user-facing messages support embed or Components V2 JSON)\n"
                f"Available panel keys: {', '.join(PANEL_KEYS)}\n\n"
                "• `?stafftools panel list` — customization status\n"
                "• `?stafftools panel show <key>` — show the current JSON\n"
                "• `?stafftools panel set <key> <JSON>` — set a panel (or attach a .json file)\n"
                "• `?stafftools panel preview <key>` — preview the result\n"
                "• `?stafftools panel reset <key>` — restore the default\n\n"
                "**Plugin configuration**\n"
                "• `?stafftools config show` — show all settings\n"
                "• `?stafftools config set <key> <value>` — change a setting"
            ),
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @stafftools.group(name="panel", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def st_panel(self, ctx: commands.Context):
        await self.stafftools(ctx)

    @st_panel.command(name="list")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def panel_list(self, ctx: commands.Context):
        custom_keys = {d.get("_id", "").replace("panel:", "") for d in await self.store.all_panels()}
        lines = []
        for key in PANEL_KEYS:
            mark = "✏️ custom" if key in custom_keys else "(default)"
            functional = " · has functional component" if key in FUNCTIONAL_PANEL_KEYS else ""
            lines.append(f"• `{key}` — {mark}{functional}")
        embed = discord.Embed(
            title="🖼 User-Facing Message Panels",
            description="\n".join(lines),
            color=self.bot.main_color,
        )
        embed.set_footer(text="Set: ?stafftools panel set <key> <JSON> · placeholders documented in the README")
        await ctx.send(embed=embed)

    @st_panel.command(name="show")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def panel_show(self, ctx: commands.Context, key: str):
        if key not in PANEL_KEYS:
            await self._unknown_panel_key(ctx, key)
            return
        doc = await self.store.get_panel(key)
        if doc:
            fmt, data = doc.get("format"), doc.get("data")
        else:
            fmt, data = resolve_panel_source(None, key)
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(data)
        if len(text) > 1800:
            text = text[:1800] + "\n…(truncated)"
        embed = discord.Embed(
            title=f"🖼 Panel `{key}` ({fmt})",
            description=f"```json\n{text}\n```",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @st_panel.command(name="set")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def panel_set(self, ctx: commands.Context, key: str, *, raw: Optional[str] = None):
        if key not in PANEL_KEYS:
            await self._unknown_panel_key(ctx, key)
            return
        payload = None
        if ctx.message.attachments:
            try:
                payload = json.loads((await ctx.message.attachments[0].read()).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                embed = discord.Embed(
                    title="❌ JSON parse error", description=f"The attachment is not valid UTF-8 JSON: {exc}",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
        else:
            text = strip_codeblock(raw)
            if not text:
                embed = discord.Embed(
                    title="❌ Missing JSON",
                    description=(
                        "Usage: `?stafftools panel set <key> <JSON>` — you can also attach the JSON as a .json file.\n"
                        'Supports the embed format (e.g. `{"embed": {"title": ...}}`) or a Components V2 array.'
                    ),
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
            try:
                payload = json.loads(text)
            except ValueError as exc:
                embed = discord.Embed(
                    title="❌ JSON parse error", description=f"```{exc}```", color=self.bot.error_color
                )
                await ctx.send(embed=embed)
                return

        fmt = detect_panel_format(payload)
        if fmt is None:
            embed = discord.Embed(
                title="❌ Unrecognized panel format",
                description=(
                    "Two formats are supported:\n"
                    '1. **embed**: a single embed object, or `{"content": "...", "embeds": [...]}`\n'
                    "2. **components**: a Components V2 component array (types 1/2/3/9/10/11/12/13/14/17)"
                ),
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        try:
            # Dry-run render to validate the structure
            render_panel(fmt, payload, {}, self.bot)
        except Exception as exc:
            embed = discord.Embed(
                title="❌ Panel validation failed", description=f"```{exc}```", color=self.bot.error_color
            )
            await ctx.send(embed=embed)
            return
        await self.store.set_panel(key, fmt, payload)
        embed = discord.Embed(
            title="✅ Panel updated",
            description=f"Panel `{key}` is now a custom {fmt} panel. Preview: `?stafftools panel preview {key}`",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @st_panel.command(name="preview")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def panel_preview(self, ctx: commands.Context, key: str):
        if key not in PANEL_KEYS:
            await self._unknown_panel_key(ctx, key)
            return
        mapping = self.build_placeholders(
            thread=None,
            staff=ctx.author,
            department="Example Department",
            duration="24 hours",
        )
        mapping["{user}"] = ctx.author.mention
        mapping["{channel}"] = ctx.channel.mention
        try:
            fmt, data = resolve_panel_source(await self.store.get_panel(key), key)
            rendered = render_panel(fmt, data, mapping, self.bot)
            if key in FUNCTIONAL_PANEL_KEYS:
                attach = "close" if key == "closerequest" else "dept"
                if attach == "dept" and not (self.store.config.get("departments") or {}):
                    raise ValueError("No departments configured — cannot preview the department select (config set departments)")
                self._attach_functional(rendered, attach, ctx.channel.id, ctx.author.id)
            if rendered["format"] == "components":
                await ctx.send(view=rendered["layout"])
            else:
                kwargs: Dict[str, Any] = {}
                if rendered["content"]:
                    kwargs["content"] = rendered["content"]
                if rendered["embeds"]:
                    kwargs["embeds"] = rendered["embeds"]
                if rendered.get("view"):
                    kwargs["view"] = rendered["view"]
                await ctx.send(**kwargs)
        except Exception as exc:
            embed = discord.Embed(
                title="❌ Preview failed", description=f"```{exc}```", color=self.bot.error_color
            )
            await ctx.send(embed=embed)

    @st_panel.command(name="reset")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def panel_reset(self, ctx: commands.Context, key: str):
        if key not in PANEL_KEYS:
            await self._unknown_panel_key(ctx, key)
            return
        await self.store.delete_panel(key)
        embed = discord.Embed(
            title="✅ Panel restored", description=f"Panel `{key}` was restored to the default template.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    async def _unknown_panel_key(self, ctx: commands.Context, key: str):
        embed = discord.Embed(
            title="❌ Unknown panel key",
            description=f"Available panel keys: {', '.join(f'`{k}`' for k in PANEL_KEYS)}",
            color=self.bot.error_color,
        )
        await ctx.send(embed=embed)

    @stafftools.group(name="config", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def st_config(self, ctx: commands.Context):
        await self._send_config_overview(ctx)

    @st_config.command(name="show")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def config_show(self, ctx: commands.Context):
        await self._send_config_overview(ctx)

    @st_config.command(name="set")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def config_set(self, ctx: commands.Context, key: str, *, value: Optional[str] = None):
        cfg = self.store.config
        if key in ("inactivity_hours", "report_days", "escalate_interval_hours"):
            bounds = {"inactivity_hours": (1, 8760), "report_days": (1, 365),
                      "escalate_interval_hours": (MIN_ESCALATE_INTERVAL, MAX_ESCALATE_INTERVAL)}[key]
            try:
                num = int((value or "").strip())
            except (TypeError, ValueError):
                num = None
            if num is None or not bounds[0] <= num <= bounds[1]:
                embed = discord.Embed(
                    title="❌ Invalid number",
                    description=f"`{key}` must be an integer between {bounds[0]} and {bounds[1]}.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed)
                return
            cfg[key] = num
        elif key in ("cancel_on_reply", "clear_escalation_on_claim", "require_departments"):
            parsed = parse_bool(value)
            if parsed is None:
                embed = discord.Embed(
                    title="❌ Invalid value", description=f"`{key}` must be true / false.", color=self.bot.error_color
                )
                await ctx.send(embed=embed)
                return
            cfg[key] = parsed
        elif key == "escalate_role_id":
            if ctx.message.role_mentions:
                cfg[key] = ctx.message.role_mentions[0].id
            elif value and str(value).strip().lower() in ("none", "clear", "off"):
                cfg[key] = None
            else:
                raw = (value or "").strip().lstrip("<@&>").rstrip(">")
                try:
                    cfg[key] = int(raw)
                except (TypeError, ValueError):
                    embed = discord.Embed(
                        title="❌ Invalid role", description="Please @mention a role or provide a role ID (`none` to clear).", color=self.bot.error_color
                    )
                    await ctx.send(embed=embed)
                    return
        elif key == "departments":
            payload = None
            if ctx.message.attachments:
                try:
                    payload = json.loads((await ctx.message.attachments[0].read()).decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    await ctx.send(embed=discord.Embed(title="❌ JSON parse error", description=str(exc), color=self.bot.error_color))
                    return
            else:
                try:
                    payload = json.loads(strip_codeblock(value))
                except ValueError as exc:
                    await ctx.send(embed=discord.Embed(
                        title="❌ JSON parse error",
                        description=f'{exc}\nExample: `?stafftools config set departments {{"Sales": 123, "Support": 456}}`',
                        color=self.bot.error_color,
                    ))
                    return
            if not isinstance(payload, dict):
                await ctx.send(embed=discord.Embed(
                    title="❌ Invalid format", description="departments must be a JSON object: {department name: category ID}",
                    color=self.bot.error_color,
                ))
                return
            cleaned: Dict[str, int] = {}
            for name, cat_id in payload.items():
                try:
                    cleaned[str(name)] = int(cat_id)
                except (TypeError, ValueError):
                    continue
            cfg[key] = cleaned
        elif key == "staff_role_ids":
            roles = list(ctx.message.role_mentions)
            if not roles and value:
                ids = []
                for token in strip_codeblock(value).replace(",", " ").split():
                    token = token.lstrip("<@&>").rstrip(">")
                    if token.isdigit():
                        ids.append(int(token))
                cfg[key] = ids
            else:
                cfg[key] = [r.id for r in roles]
        else:
            embed = discord.Embed(
                title="❌ Unknown setting",
                description=(
                    "Configurable settings:\n• `inactivity_hours` (default auto-close hours)\n"
                    "• `cancel_on_reply` (a user reply cancels the auto-close)\n"
                    "• `escalate_role_id` / `escalate_interval_hours`\n"
                    "• `clear_escalation_on_claim`\n"
                    "• `departments` / `require_departments`\n"
                    "• `report_days` / `staff_role_ids`"
                ),
                color=self.bot.error_color,
            )
            await ctx.send(embed=embed)
            return
        await self.store.save_config()
        await self._send_config_overview(ctx, highlight=key)

    async def _send_config_overview(self, ctx: commands.Context, highlight: Optional[str] = None):
        cfg = self.store.config
        role_id = cfg.get("escalate_role_id")
        departments = cfg.get("departments") or {}
        dep_text = (
            "\n".join(f"• {k} → <#{v}>" for k, v in list(departments.items())[:15])
            if departments
            else "(not configured)"
        )
        roles = cfg.get("staff_role_ids") or []
        lines = [
            f"• `inactivity_hours`: {cfg.get('inactivity_hours')} hour(s)"
            f"{'  ← just changed' if highlight == 'inactivity_hours' else ''}",
            f"• `cancel_on_reply`: {cfg.get('cancel_on_reply')}",
            f"• `escalate_role_id`: <@&{role_id}>" if role_id else "• `escalate_role_id`: (not configured)",
            f"• `escalate_interval_hours`: {cfg.get('escalate_interval_hours')} hour(s)",
            f"• `clear_escalation_on_claim`: {cfg.get('clear_escalation_on_claim')}",
            f"• `require_departments`: {cfg.get('require_departments')}",
            f"• `report_days`: {cfg.get('report_days')} day(s)",
            f"• `staff_role_ids`: {' '.join(f'<@&{r}>' for r in roles) if roles else '(automatic from permission levels)'}",
        ]
        embed = discord.Embed(
            title="⚙️ StaffTools Configuration", description="\n".join(lines), color=self.bot.main_color
        )
        embed.add_field(name="departments", value=dep_text, inline=False)
        embed.set_footer(text="Change: ?stafftools config set <key> <value>")
        await ctx.send(embed=embed)


async def setup(bot):
    """Plugin entry point: register the Cog and detect foreign command conflicts."""
    cog = StaffTools(bot)

    def _root_name(cmd):
        node = cmd
        seen = set()
        while True:
            parent = getattr(node, "parent", None)
            if parent is None or id(parent) in seen:
                break
            seen.add(id(node))
            node = parent
        return getattr(node, "name", None)

    foreign = [n for n in TOP_LEVEL_COMMANDS if bot.get_command(n) is not None]
    if foreign:
        logger.warning(
            "Command(s) %s already provided by the core or another plugin — StaffTools will skip "
            "registering them (the related counting features will be unavailable).",
            ", ".join(foreign),
        )
        cog.__cog_commands__ = tuple(
            c for c in cog.__cog_commands__ if _root_name(c) not in foreign
        )
    await bot.add_cog(cog)

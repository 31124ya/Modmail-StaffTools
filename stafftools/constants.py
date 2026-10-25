# -*- coding: utf-8 -*-
"""
StaffTools plugin constants, default configuration and default panel templates.

Highlights since v2.0:
- Thread-level operation commands (closerequest / escalate / onhold / resume / transfer)
- Every message sent to the user can be customized with JSON (embed or Components V2)
- ?inactivity is a "no-reply auto-close" timer (24 hours by default)
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN_NAME = "StaffTools"
PLUGIN_REPO = "https://github.com/yourname/modmail-stafftools"

# Read plugin metadata (version, description) from info.json.
# Prefer the repository-root copy, fall back to the package copy.
_INFO_CANDIDATES = (
    Path(__file__).parent.resolve().parent / "info.json",
    Path(__file__).parent.resolve() / "info.json",
)
_info: dict = {}
for _candidate in _INFO_CANDIDATES:
    try:
        with open(_candidate, encoding="utf-8") as f:
            _info = json.loads(f.read())
        break
    except (OSError, ValueError):
        continue

VERSION: str = _info.get("version", "2.0.0")
DESCRIPTION: str = " ".join(_info.get("description", []))

# ---------------------------------------------------------------------------
# Database document markers (stored in the plugin-specific MongoDB partition
# provided by bot.api.get_plugin_partition)
# ---------------------------------------------------------------------------
DOC_CONFIG = "config"          # single doc _id="config": the whole plugin config
DOC_MEMBER = "member"          # member stat doc: _id=<user_id>
DOC_META = "meta"              # runtime metadata: _id="meta" (digest timestamp etc.)
THREAD_ID_PREFIX = "thread:"   # thread state doc _id: thread:<channel_id>
PANEL_ID_PREFIX = "panel:"     # custom panel doc _id: panel:<key>

# ---------------------------------------------------------------------------
# Default configuration values
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # Default duration (hours) for ?inactivity auto-close; can be overridden per command
    "inactivity_hours": 24,
    # Whether a user reply automatically cancels the inactivity auto-close timer
    "cancel_on_reply": True,
    # Target role ID for escalation digests; None means not configured
    "escalate_role_id": None,
    # Interval (hours) between escalation digests
    "escalate_interval_hours": 24,
    # Whether a senior staff ?claim automatically clears the escalation flag
    "clear_escalation_on_claim": True,
    # Department map: {department name: category ID}; empty = not configured
    "departments": {},
    # Whether users are DM'd a department select menu when a ticket is created
    "require_departments": True,
    # ?inactivity report activity threshold (days)
    "report_days": 7,
    # Staff role whitelist; empty = derived from Modmail permission levels
    "staff_role_ids": [],
}

# Inactivity timer bounds (seconds): 10 minutes ~ 30 days
MIN_INACTIVITY_SECONDS = 10 * 60
MAX_INACTIVITY_SECONDS = 30 * 86400

# Escalation digest interval bounds (hours): 1 ~ 168
MIN_ESCALATE_INTERVAL = 1
MAX_ESCALATE_INTERVAL = 168

# ---------------------------------------------------------------------------
# Interactive component custom_id conventions (prefix "st:" is handled by the
# plugin's global on_interaction listener)
# ---------------------------------------------------------------------------
CUSTOM_ID_PREFIX = "st:"
CID_CLOSE_REQUEST = "cr"    # st:cr:<channel_id>:<requester_id>  close confirm button
CID_DEPT_SELECT = "dept"    # st:dept:<channel_id>               department select menu

# ---------------------------------------------------------------------------
# Counter field names (shared by the database and the display layer)
# ---------------------------------------------------------------------------
FIELD_CLOSES = "closes"
FIELD_CLAIMS = "claims"
FIELD_TRANSFERS = "transfers"
FIELD_REPLIES = "replies"
COUNT_FIELDS = (FIELD_CLOSES, FIELD_CLAIMS, FIELD_TRANSFERS, FIELD_REPLIES)
# Fields shown in (and used to sort) the ?stats leaderboard (order = priority)
LEADERBOARD_FIELDS = (FIELD_CLOSES, FIELD_CLAIMS, FIELD_TRANSFERS)

# Leaderboard size limit
LEADERBOARD_SIZE = 10
# Max member rows listed by ?inactivity report (extra rows are folded)
INACTIVITY_REPORT_MAX_ROWS = 25

# Modmail permission level names whose bound roles count as staff
# (used when no explicit staff_role_ids are configured)
STAFF_PERMISSION_LEVELS = ("SUPPORTER", "MODERATOR", "ADMINISTRATOR", "OWNER")

# Leaderboard medals
MEDALS = ("🥇", "🥈", "🥉")

# ---------------------------------------------------------------------------
# User-facing message panels (overridable via ?stafftools panel set <key> JSON)
# ---------------------------------------------------------------------------
# Panel key -> purpose:
#   claim             DM sent to the user when a staff member runs ?claim
#   inactivity        DM sent to the user when ?inactivity schedules an auto-close
#   closerequest      close-confirmation panel sent to the user (with the close button)
#   escalate          DM sent to the user when the ticket is escalated
#   onhold            DM sent to the user when the ticket is put on hold
#   resume            DM sent to the user when the ticket is resumed
#   transfer          DM sent to the user after ?transfer / after picking a department
#   department_select department select menu DM sent to the user on ticket creation
PANEL_KEYS = (
    "claim",
    "inactivity",
    "closerequest",
    "escalate",
    "onhold",
    "resume",
    "transfer",
    "department_select",
)

# These panels get a functional interactive component (button / select) appended
# after rendering
FUNCTIONAL_PANEL_KEYS = frozenset({"closerequest", "department_select"})

# Available placeholders (substituted in every string of the panel JSON)
PLACEHOLDER_DOCS = {
    "{user}": "user mention",
    "{user_name}": "user display name",
    "{staff}": "acting staff display name",
    "{staff_mention}": "acting staff mention",
    "{department}": "department name",
    "{duration}": "duration text (e.g. 24 hours)",
    "{guild}": "guild name",
    "{channel}": "ticket channel mention",
}

# Default panel templates (embed format; when colour is omitted the renderer
# fills in bot.main_color)
DEFAULT_PANELS = {
    "claim": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "🤝 Ticket Claimed",
                "description": "{staff_mention} has claimed your ticket and will keep assisting you from here.",
            }
        },
    },
    "inactivity": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "⏳ Awaiting Your Reply",
                "description": (
                    "{staff_mention} is waiting for your reply.\n\n"
                    "If we do not hear back from you within **{duration}**, "
                    "this ticket will be closed automatically. To keep it open, "
                    "just reply to this message."
                ),
            }
        },
    },
    "closerequest": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "🎫 Close Ticket Confirmation",
                "description": (
                    "Staff member {staff_mention} has requested to close this ticket.\n"
                    "If your issue has been resolved, please click the button below to confirm."
                ),
            }
        },
    },
    "escalate": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "📣 Ticket Escalated",
                "description": "Your ticket has been escalated to our senior staff team. We will get back to you as soon as possible.",
            }
        },
    },
    "onhold": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "⏸ Ticket On Hold",
                "description": "Your ticket has been placed on hold. We will notify you as soon as it is resumed.",
            }
        },
    },
    "resume": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "▶️ Ticket Resumed",
                "description": "Your ticket is no longer on hold. We will get back to you shortly.",
            }
        },
    },
    "transfer": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "🔀 Ticket Transferred",
                "description": "Your ticket has been transferred to the **{department}** department. A member of that team will assist you shortly.",
            }
        },
    },
    "department_select": {
        "format": "embed",
        "data": {
            "embed": {
                "title": "🗂 Please Choose a Department",
                "description": (
                    "To route your issue faster, please select the department you need "
                    "from the menu below.\nYour ticket will be handled by the corresponding team."
                ),
            }
        },
    },
}

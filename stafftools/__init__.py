"""
StaffTools — a Modmail plugin for staff tooling and statistics.

Claiming / co-claiming, close requests, no-reply auto-close, escalation digests,
hold / resume, department transfers and a claims / closes / transfers leaderboard.
All user-facing messages can be customized with embed or Components V2 JSON panels.
"""

from .stafftools import StaffTools, setup

__all__ = ("StaffTools", "setup")

# StaffTools

Staff tooling & statistics plugin for Modmail — claiming & co-working, close requests,
no-reply auto-close, escalation digests, hold / resume, department transfers, and a
**claims / closes / transfers** statistics leaderboard for the whole team.

Every message sent to the user supports a custom **embed JSON or Components V2 JSON** panel.

- Plugin name: `stafftools`
- Compatibility: Modmail master (discord.py ≥ 2.6, required for Components V2 panels)
- Storage: reuses Modmail's built-in MongoDB (`get_plugin_partition` plugin partition) — zero extra setup

## Installation

```text
?plugin add <your-github-username>/modmail-stafftools/stafftools
```

> If another plugin (e.g. the official registry Claim plugin) also provides `?claim`,
> this plugin automatically skips the conflicting commands and logs a warning; all
> other features keep working.

## Commands

### Claiming & co-working

| Command | Description |
| --- | --- |
| `?claim` | Claim the current ticket (claims +1) and DM the user a custom panel |
| `?unclaim` | Release your claim (claimant only; stats are not rolled back) |
| `?addclaim @member` | Add a co-claimant (their claims +1; records who added them) |
| `?removeclaim @member` | Remove a co-claimant (claims -1; only the adder or an administrator) |

### Closing

| Command | Description |
| --- | --- |
| `?closerequest` | Send the user a close-confirmation panel with a button; **when the user clicks it, the close counts toward the staff member who sent the request** |
| `?inactivity [duration]` | Schedule a no-reply auto-close, 24h by default (`?inactivity 48h` / `2d` / `90m`); **when it fires, the close counts toward the scheduler** |
| `?inactivity cancel` | Cancel the auto-close on the current ticket |
| `?inactivity report` | Staff activity report (sorted by last active time) |
| `?inactivity set <days>` | Activity report threshold (administrator) |
| `?inactivity roles @role` | Staff roles tracked by the report (administrator) |

> A user reply during the timer automatically cancels the close
> (disable via the `cancel_on_reply` setting).

### Escalate / hold / transfer

| Command | Description |
| --- | --- |
| `?escalate` | Flag the ticket for senior staff review |
| `?onhold` / `?resume` | Hold / resume the ticket (both DM the user a custom panel) |
| `?transfer <department>` | Move the ticket channel to the department category (transfers +1) |

### Statistics

| Command | Description |
| --- | --- |
| `?stats` | Leaderboard: per-member **claims / closes / transfers** |
| `?stats me` / `?stats user @member` | Personal statistics card |
| `?stats reset confirm` | Wipe all statistics (guild owner) |

### Management

| Command | Description |
| --- | --- |
| `?stafftools panel list/show/set/preview/reset` | Manage user-facing message panels |
| `?stafftools config show/set` | Manage plugin settings |

## Counting rules

- **closes**: every staff-attributed close counts +1 — including the core `?close`,
  `?closerequest` confirmed by the user (attributed to the requester), and the
  `?inactivity` auto-close (attributed to the scheduler).
  User self-closes and bot closes are not counted.
- **claims**: `?claim` +1; `?addclaim` +1 for the added member; `?removeclaim` -1 for the removed member.
- **transfers**: `?transfer` +1 per transfer (the user's department choice at ticket creation does not count).
- **replies**: +1 per staff reply inside a ticket (used by the activity report; not shown on the leaderboard).

## Settings

```text
?stafftools config set inactivity_hours 24          # default ?inactivity duration (hours)
?stafftools config set cancel_on_reply true         # a user reply cancels the auto-close
?stafftools config set escalate_role_id @senior     # escalation digest target role
?stafftools config set escalate_interval_hours 24   # digest interval (hours)
?stafftools config set clear_escalation_on_claim true  # a senior ?claim clears the escalation flag
?stafftools config set require_departments true     # force a department choice on ticket creation
?stafftools config set departments {"Sales": 111, "Support": 222}   # department name -> category ID
?stafftools config set report_days 7                # activity report threshold (days)
?stafftools config set staff_role_ids @staff @lead  # report staff roles (empty = derived from permission levels)
```

## Escalation digest

1. `?stafftools config set escalate_role_id @senior-staff`
2. Staff flag a ticket with `?escalate`
3. Every `escalate_interval_hours` (default 24h) the bot DMs every member holding the
   role, listing the pending escalated tickets (channel, user, department, escalator,
   when); **nothing is sent when the list is empty**
4. The flag clears automatically when a senior staff member `?claim`s the ticket
   (`clear_escalation_on_claim`) or when the ticket is closed

## Department transfers

1. Create a Discord category (Category Channel) for each department
2. Configure: `?stafftools config set departments {"Sales": <category-id>, "Support": <category-id>}`
3. With `require_departments` enabled, every new ticket DMs the user a department select
   panel; picking one moves the ticket channel into the matching category
   (unpicked tickets stay in the default category)
4. Staff can `?transfer Support` at any time (transfers +1)

## User-facing message panels (embed / Components V2 JSON)

8 panel keys cover every message sent to the user:

`claim` · `inactivity` · `closerequest` · `escalate` · `onhold` · `resume` · `transfer` · `department_select`

```text
?stafftools panel show <key>       # show the current JSON (or the default template)
?stafftools panel set <key> <JSON> # set it (inline or as an attached .json file)
?stafftools panel preview <key>    # preview it in the current channel
?stafftools panel reset <key>      # restore the default
```

### Available placeholders

| Placeholder | Meaning |
| --- | --- |
| `{user}` / `{user_name}` | user mention / display name |
| `{staff}` / `{staff_mention}` | acting staff display name / mention |
| `{department}` | department name |
| `{duration}` | duration text (e.g. `24 hours`) |
| `{guild}` | guild name |
| `{channel}` | ticket channel mention |

### embed format example (claim)

```json
{"embed": {"title": "🤝 Someone is on it", "description": "{staff_mention} is handling your ticket now.", "color": 5793266}}
```

### Components V2 format example (claim)

```json
[
  {"type": 17, "accent_color": 5793266, "components": [
    {"type": 10, "content": "## 🤝 Ticket claimed\n{staff_mention} will assist you"},
    {"type": 9, "components": [{"type": 10, "content": "-# Thanks for your patience"}],
     "accessory": {"type": 11, "media": {"url": "https://example.com/avatar.png"}}}
  ]}
]
```

Supported component types: `1` ActionRow, `2` Button, `3` Select, `9` Section,
`10` TextDisplay, `11` Thumbnail, `12` MediaGallery, `13` File, `14` Separator, `17` Container.

> **closerequest / department_select** panels automatically get the functional button /
> select appended. If your custom JSON already contains a component whose `custom_id`
> starts with `st:cr:` / `st:dept:`, the plugin will not append a duplicate.
> Components V2 requires discord.py ≥ 2.6 (bundled with Modmail master).

## FAQ

**Who gets the close counted when ?inactivity fires?** The staff member who ran
`?inactivity`. If they have left the guild, the ticket still closes but is not counted.

**What if a user never picks a department?** The ticket stays in the default category;
staff can `?transfer` it at any time.

**Switching databases / wiping stats?** `?stats reset confirm` wipes the counters;
thread state is cleaned up automatically on close.

**Panel JSON fails validation?** `panel set` performs a full dry-run render before
saving — fix the JSON according to the error; `panel preview` shows the final result
right in the channel.

## License

MIT

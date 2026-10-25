# -*- coding: utf-8 -*-
"""
StaffTools user-facing message panel renderer.

Supports two JSON formats (set via ?stafftools panel set <key>):

1. embed format — a single embed object, or {"content": "...", "embeds": [embed, ...]}
2. components format — a Discord Components V2 component array (also accepts
   {"components": [...]} or a single component object); rendered as a
   discord.ui.LayoutView

Every string field supports placeholder substitution (see constants.PLACEHOLDER_DOCS).
Functional components (close button / department select) are appended by the Cog
onto the rendered result; they carry no callback here — interactions are routed
through the Cog's global on_interaction listener.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import discord

from .constants import DEFAULT_PANELS

# Components V2 / interactive component type codes
_T_ACTION_ROW = 1
_T_BUTTON = 2
_T_SELECT = 3
_T_SECTION = 9
_T_TEXT_DISPLAY = 10
_T_THUMBNAIL = 11
_T_MEDIA_GALLERY = 12
_T_FILE = 13
_T_SEPARATOR = 14
_T_CONTAINER = 17

_INTERACTIVE_TYPES = (discord.ui.Button, discord.ui.Select)
_KNOWN_TYPES = (
    _T_ACTION_ROW,
    _T_BUTTON,
    _T_SELECT,
    _T_SECTION,
    _T_TEXT_DISPLAY,
    _T_THUMBNAIL,
    _T_MEDIA_GALLERY,
    _T_FILE,
    _T_SEPARATOR,
    _T_CONTAINER,
)

_BUTTON_STYLE_MAP = {
    1: discord.ButtonStyle.primary,
    2: discord.ButtonStyle.secondary,
    3: discord.ButtonStyle.success,
    4: discord.ButtonStyle.danger,
    5: discord.ButtonStyle.link,
}

_EMBED_HINT_KEYS = (
    "title",
    "description",
    "fields",
    "color",
    "colour",
    "author",
    "footer",
    "thumbnail",
    "image",
    "timestamp",
)

_TOKEN_RE = re.compile(r"\{[a-z_]+\}")


# ---------------------------------------------------------------------------
# Placeholder substitution
# ---------------------------------------------------------------------------
def substitute(text: str, mapping: Dict[str, str]) -> str:
    """Replace {token} placeholders in text with values from mapping."""
    if not isinstance(text, str) or not mapping:
        return text

    def _sub(match: "re.Match[str]") -> str:
        token = match.group(0)
        return mapping.get(token, token)

    return _TOKEN_RE.sub(_sub, text)


def deep_sub(obj: Any, mapping: Dict[str, str]) -> Any:
    """Recursively apply placeholder substitution to dict / list / str."""
    if isinstance(obj, str):
        return substitute(obj, mapping)
    if isinstance(obj, dict):
        return {k: deep_sub(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_sub(v, mapping) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------
def detect_format(payload: Any) -> Optional[str]:
    """Detect whether the JSON payload is embed or components format. None if unknown."""
    if isinstance(payload, list):
        if payload and all(isinstance(x, dict) for x in payload):
            return "components"
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("components"), list):
        return "components"
    t = payload.get("type")
    if isinstance(t, int) and t in _KNOWN_TYPES:
        return "components"
    if "embed" in payload or "embeds" in payload:
        return "embed"
    if any(k in payload for k in _EMBED_HINT_KEYS):
        return "embed"
    return None


# ---------------------------------------------------------------------------
# embed format rendering
# ---------------------------------------------------------------------------
def build_embed_payload(data: Any) -> Tuple[Optional[str], List[discord.Embed]]:
    """Convert embed-format JSON into (content, embeds).

    Accepts: a single embed object / {"embed": {...}} / {"embeds": [...], "content": "..."}
    """
    content: Optional[str] = None
    embed_dicts: List[Dict[str, Any]] = []
    if isinstance(data, dict) and ("embed" in data or "embeds" in data or "content" in data):
        content = data.get("content")
        raw_embeds = data.get("embeds")
        if raw_embeds is None and "embed" in data:
            raw_embeds = [data["embed"]]
        if isinstance(raw_embeds, dict):
            raw_embeds = [raw_embeds]
        if isinstance(raw_embeds, list):
            embed_dicts = [e for e in raw_embeds if isinstance(e, dict)]
    elif isinstance(data, dict):
        embed_dicts = [data]
    embeds = [discord.Embed.from_dict(e) for e in embed_dicts[:10]]  # Discord caps at 10
    if content is not None:
        content = str(content)
    return content, embeds


# ---------------------------------------------------------------------------
# Components V2 rendering
# ---------------------------------------------------------------------------
def _media_url(value: Any) -> str:
    """Accept the media field as {"url": ...} or a plain URL string."""
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value:
        raise ValueError('the "media" field requires a URL string or a {"url": ...} object')
    return value


def _emoji(value: Any) -> Optional[str]:
    """Accept the emoji field as "😀" / {"name": "..."} / {"name","id","animated"}."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        if not name:
            return None
        emoji_id = value.get("id")
        if emoji_id:
            animated = "a" if value.get("animated") else ""
            return f"<{animated}:{name}:{emoji_id}>"
        return str(name)
    return str(value)


def _spacing(value: Any) -> discord.SeparatorSpacing:
    try:
        return discord.SeparatorSpacing.large if int(value) == 2 else discord.SeparatorSpacing.small
    except (TypeError, ValueError):
        return discord.SeparatorSpacing.small


def build_component(node: Any):
    """Recursively build a single component JSON node into a discord.ui item."""
    if not isinstance(node, dict):
        raise ValueError("a component node must be a JSON object")
    ctype = node.get("type")
    if ctype not in _KNOWN_TYPES:
        raise ValueError(f"unsupported component type: {ctype!r}")

    if ctype == _T_TEXT_DISPLAY:
        content = node.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("TextDisplay (type 10) requires a content string")
        return discord.ui.TextDisplay(content)

    if ctype == _T_SEPARATOR:
        return discord.ui.Separator(
            visible=bool(node.get("divider", True)),
            spacing=_spacing(node.get("spacing", 1)),
        )

    if ctype == _T_FILE:
        return discord.ui.File(
            _media_url(node.get("media")),
            spoiler=bool(node.get("spoiler", False)),
        )

    if ctype == _T_MEDIA_GALLERY:
        items = node.get("items") or []
        if not items:
            raise ValueError("MediaGallery (type 12) requires at least one item")
        gallery_items = [
            discord.MediaGalleryItem(
                media=_media_url(item.get("media")),
                description=item.get("description"),
                spoiler=bool(item.get("spoiler", False)),
            )
            for item in items
            if isinstance(item, dict)
        ]
        return discord.ui.MediaGallery(*gallery_items)

    if ctype == _T_THUMBNAIL:
        return discord.ui.Thumbnail(
            _media_url(node.get("media")),
            description=node.get("description"),
            spoiler=bool(node.get("spoiler", False)),
        )

    if ctype == _T_BUTTON:
        url = node.get("url")
        style = _BUTTON_STYLE_MAP.get(node.get("style", 5 if url else 1))
        if style is None:
            raise ValueError(f"unknown button style: {node.get('style')!r}")
        return discord.ui.Button(
            style=style,
            label=node.get("label"),
            custom_id=node.get("custom_id"),
            url=url,
            emoji=_emoji(node.get("emoji")),
            disabled=bool(node.get("disabled", False)),
        )

    if ctype == _T_SELECT:
        raw_options = node.get("options") or []
        if not raw_options:
            raise ValueError("a select menu (type 3) requires at least one option")
        options = []
        for opt in raw_options[:25]:
            if not isinstance(opt, dict) or not opt.get("label"):
                raise ValueError("each select option requires a label field")
            options.append(
                discord.SelectOption(
                    label=str(opt["label"]),
                    value=str(opt.get("value", opt["label"])),
                    description=opt.get("description"),
                    emoji=_emoji(opt.get("emoji")),
                    default=bool(opt.get("default", False)),
                )
            )
        return discord.ui.Select(
            custom_id=node.get("custom_id"),
            placeholder=node.get("placeholder"),
            min_values=min(int(node.get("min_values", 1)), len(options)),
            max_values=min(int(node.get("max_values", 1)), len(options)),
            options=options,
            disabled=bool(node.get("disabled", False)),
        )

    if ctype == _T_SECTION:
        children_raw = node.get("components") or []
        if not children_raw:
            raise ValueError("Section (type 9) requires text components")
        children = [build_component(c) for c in children_raw[:3]]
        accessory_raw = node.get("accessory")
        if not accessory_raw:
            raise ValueError("Section (type 9) requires an accessory (thumbnail or button)")
        accessory = build_component(accessory_raw)
        return discord.ui.Section(*children, accessory=accessory)

    if ctype == _T_CONTAINER:
        colour = node.get("accent_color", node.get("accent_colour"))
        children = [build_component(c) for c in (node.get("components") or [])]
        return discord.ui.Container(
            *children,
            accent_color=colour,
            spoiler=bool(node.get("spoiler", False)),
        )

    if ctype == _T_ACTION_ROW:
        children = [build_component(c) for c in (node.get("components") or [])]
        if not children:
            raise ValueError("ActionRow (type 1) requires at least one button / select child")
        return discord.ui.ActionRow(*children)

    raise ValueError(f"unsupported component type: {ctype!r}")  # pragma: no cover


def _contains_custom_id(nodes: Any, prefix: str) -> bool:
    """Recursively check whether the component tree already has an interactive
    component whose custom_id starts with the given prefix."""
    if isinstance(nodes, dict):
        cid = nodes.get("custom_id")
        if isinstance(cid, str) and cid.startswith(prefix):
            return True
        for key in ("components", "accessory", "items"):
            if _contains_custom_id(nodes.get(key), prefix):
                return True
        return False
    if isinstance(nodes, list):
        return any(_contains_custom_id(n, prefix) for n in nodes)
    return False


def build_layout(components: List[Dict[str, Any]]) -> discord.ui.LayoutView:
    """Build a component JSON array into a LayoutView."""
    view = discord.ui.LayoutView(timeout=None)
    for node in components:
        item = build_component(node)
        if isinstance(item, _INTERACTIVE_TYPES):
            # Top-level buttons / selects are not allowed — wrap in an ActionRow
            item = discord.ui.ActionRow(item)
        view.add_item(item)
    if len(view.children) == 0:
        raise ValueError("the components array is empty")
    return view


# ---------------------------------------------------------------------------
# Panel rendering entry points
# ---------------------------------------------------------------------------
def default_panel(key: str) -> Tuple[str, Any]:
    """Return the default panel template (format, data)."""
    entry = DEFAULT_PANELS.get(key) or {"format": "embed", "data": {}}
    return entry["format"], entry["data"]


def resolve_panel_source(panel_doc: Optional[Dict[str, Any]], key: str) -> Tuple[str, Any]:
    """Prefer the custom panel doc, otherwise fall back to the default template."""
    if panel_doc and panel_doc.get("format") in ("embed", "components"):
        return panel_doc["format"], panel_doc.get("data")
    return default_panel(key)


def render_panel(
    fmt: str,
    data: Any,
    mapping: Dict[str, str],
    bot=None,
) -> Dict[str, Any]:
    """Render a panel into a unified send payload.

    Returns:
    {
      "format": "embed" | "components",
      "content": str|None,          # plain text (embed format only)
      "embeds": [Embed, ...],       # embed format
      "layout": LayoutView|None,    # components format
      "raw": the original JSON (used for functional-component detection)
    }
    """
    payload = deep_sub(data, mapping)
    if fmt == "embed":
        content, embeds = build_embed_payload(payload)
        main = getattr(bot, "main_color", None)
        for embed in embeds:
            if embed.colour is None and main is not None:
                embed.colour = main
        if not embeds and not content:
            raise ValueError("the panel is empty")
        return {"format": "embed", "content": content, "embeds": embeds, "layout": None, "raw": payload}
    # components format: normalize the payload into an array
    if isinstance(payload, dict):
        comps = payload.get("components")
        if not isinstance(comps, list):
            comps = [payload]
    else:
        comps = payload
    layout = build_layout(comps)
    return {"format": "components", "content": None, "embeds": None, "layout": layout, "raw": payload}

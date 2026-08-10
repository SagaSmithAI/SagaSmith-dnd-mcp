"""Deterministic, non-authoritative combat snapshot rendering."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw, ImageFont, ImageOps

_PAPER = "#f5efdf"
_INK = "#171916"
_MUTED = "#777367"
_GRID_BG = "#20241f"
_GRID_LINE = "#666b60"
_FRIENDLY = "#637b64"
_HOSTILE = "#b64732"
_NEUTRAL = "#b08c4e"
_ACCENT = "#e2522d"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    filenames = (
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    )
    for filename in filenames:
        if Path(filename).is_file():
            try:
                return ImageFont.truetype(filename, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, value: str, font: Any, width: int) -> str:
    text = " ".join(str(value or "").split())
    if draw.textlength(text, font=font) <= width:
        return text
    suffix = "..."
    while text and draw.textlength(text + suffix, font=font) > width:
        text = text[:-1]
    return text + suffix


def _disposition_color(value: Any) -> str:
    disposition = str(value or "friendly").casefold()
    if disposition == "hostile":
        return _HOSTILE
    if disposition == "neutral":
        return _NEUTRAL
    return _FRIENDLY


def _condition_labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels = []
    for item in value:
        label = (
            item.get("name") or item.get("id") or item.get("condition_id")
            if isinstance(item, Mapping)
            else item
        )
        normalized = " ".join(str(label or "").split())
        if normalized:
            labels.append(normalized)
    return labels


def _portrait_image(content: bytes | None, size: int) -> Image.Image | None:
    if not content:
        return None
    try:
        with Image.open(BytesIO(content)) as source:
            return ImageOps.fit(
                source.convert("RGB"),
                (size, size),
                method=Image.Resampling.LANCZOS,
            )
    except (OSError, ValueError):
        return None


def _draw_round_portrait(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    center: tuple[int, int],
    diameter: int,
    actor: Mapping[str, Any],
    portrait: bytes | None,
    current: bool,
) -> None:
    left = center[0] - diameter // 2
    top = center[1] - diameter // 2
    color = _disposition_color(actor.get("disposition"))
    picture = _portrait_image(portrait, diameter)
    if picture is not None:
        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
        canvas.paste(picture, (left, top), mask)
    else:
        draw.ellipse((left, top, left + diameter, top + diameter), fill=color)
        label = str(actor.get("name") or actor.get("actor_id") or "?")[:2]
        font = _font(max(10, diameter // 3), bold=True)
        box = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (
                center[0] - (box[2] - box[0]) / 2,
                center[1] - (box[3] - box[1]) / 2,
            ),
            label,
            fill=_PAPER,
            font=font,
        )
    border = _ACCENT if current else color
    draw.ellipse(
        (left - 3, top - 3, left + diameter + 3, top + diameter + 3),
        outline=border,
        width=5 if current else 3,
    )


def _current_actor_id(combatants: list[dict[str, Any]], turn_index: Any) -> str | None:
    if isinstance(turn_index, int) and 0 <= turn_index < len(combatants):
        return str(combatants[turn_index].get("actor_id") or "") or None
    return None


def _draw_grid(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    battle_map: Mapping[str, Any],
    combatants: list[dict[str, Any]],
    portraits: Mapping[str, bytes],
    current_actor_id: str | None,
    left: int,
    top: int,
    width_cells: int,
    height_cells: int,
    cell: int,
) -> None:
    width = width_cells * cell
    height = height_cells * cell
    draw.rectangle((left, top, left + width, top + height), fill=_GRID_BG)
    difficult = set(battle_map.get("difficult_cells") or [])
    blocked = set(battle_map.get("blocked_cells") or [])
    for y in range(height_cells):
        for x in range(width_cells):
            key = f"{x},{y}"
            cell_left = left + x * cell
            cell_top = top + y * cell
            if key in difficult:
                draw.rectangle(
                    (cell_left, cell_top, cell_left + cell, cell_top + cell),
                    fill="#665333",
                )
            if key in blocked:
                draw.rectangle(
                    (cell_left, cell_top, cell_left + cell, cell_top + cell),
                    fill="#0c0d0c",
                )
                draw.line(
                    (cell_left + 2, cell_top + 2, cell_left + cell - 2, cell_top + cell - 2),
                    fill=_ACCENT,
                    width=2,
                )
    for x in range(width_cells + 1):
        pixel = left + x * cell
        draw.line((pixel, top, pixel, top + height), fill=_GRID_LINE, width=1)
    for y in range(height_cells + 1):
        pixel = top + y * cell
        draw.line((left, pixel, left + width, pixel), fill=_GRID_LINE, width=1)
    for actor in combatants:
        position = actor.get("position")
        if not isinstance(position, Mapping):
            continue
        x, y = position.get("x"), position.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            continue
        actor_id = str(actor.get("actor_id") or "")
        _draw_round_portrait(
            canvas,
            draw,
            center=(left + x * cell + cell // 2, top + y * cell + cell // 2),
            diameter=max(12, min(cell - 8, 48)),
            actor=actor,
            portrait=portraits.get(actor_id),
            current=actor_id == current_actor_id,
        )


def _draw_agent_panel(
    draw: ImageDraw.ImageDraw,
    *,
    left: int,
    top: int,
    width: int,
    height: int,
    body_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    draw.rounded_rectangle(
        (left, top, left + width, top + height),
        radius=18,
        fill=_GRID_BG,
    )
    draw.text(
        (left + width // 2, top + height // 2 - 24),
        "AGENT SPATIAL ADJUDICATION",
        fill=_PAPER,
        font=_font(30, bold=True),
        anchor="mm",
    )
    draw.text(
        (left + width // 2, top + height // 2 + 24),
        "No grid or token coordinates are authoritative in this encounter.",
        fill="#adb2a8",
        font=body_font,
        anchor="mm",
    )


def render_combat_png(
    encounter: Mapping[str, Any],
    *,
    portraits: Mapping[str, bytes] | None = None,
    audience_projection: str,
) -> tuple[dict[str, Any], bytes]:
    """Render one already audience-filtered encounter to a PNG plus metadata."""

    value = dict(encounter)
    combatants = [
        dict(item) for item in value.get("combatants", []) if isinstance(item, Mapping)
    ]
    current_actor_id = _current_actor_id(combatants, value.get("turn_index"))
    positioning_mode = str(value.get("positioning_mode") or "agent").casefold()
    battle_map = dict(value.get("battle_map") or {})
    bounds = dict(battle_map.get("bounds") or {})
    width_cells = int(bounds.get("width_cells", 0) or 0)
    height_cells = int(bounds.get("height_cells", 0) or 0)
    grid_mode = positioning_mode == "grid" and width_cells > 0 and height_cells > 0

    cell = max(8, min(64, 1120 // width_cells, 800 // height_cells)) if grid_mode else 0
    map_width = width_cells * cell if grid_mode else 960
    map_height = height_cells * cell if grid_mode else 720
    roster_width = 430
    header_height = 132
    footer_height = 70
    roster_height = max(300, len(combatants) * 78 + 54)
    canvas_width = max(1180, map_width + roster_width + 90)
    canvas_height = max(
        760,
        header_height + map_height + footer_height,
        header_height + roster_height,
    )
    canvas = Image.new("RGB", (canvas_width, canvas_height), _PAPER)
    draw = ImageDraw.Draw(canvas)
    title_font = _font(38, bold=True)
    meta_font = _font(18, bold=True)
    body_font = _font(18)
    small_font = _font(14)

    draw.text((42, 30), str(value.get("name") or "Combat"), fill=_INK, font=title_font)
    round_label = f"ROUND {int(value.get('round', 1) or 1)}"
    mode_label = "GRID POSITIONING" if grid_mode else "AGENT POSITIONING - NO COORDINATES"
    draw.text((44, 82), f"{round_label} - {mode_label}", fill=_ACCENT, font=meta_font)
    draw.text(
        (canvas_width - 42, 46),
        str(audience_projection).upper(),
        fill=_MUTED,
        font=small_font,
        anchor="ra",
    )

    map_left = 42
    map_top = header_height
    portrait_values = portraits or {}
    if grid_mode:
        _draw_grid(
            canvas,
            draw,
            battle_map=battle_map,
            combatants=combatants,
            portraits=portrait_values,
            current_actor_id=current_actor_id,
            left=map_left,
            top=map_top,
            width_cells=width_cells,
            height_cells=height_cells,
            cell=cell,
        )
    else:
        _draw_agent_panel(
            draw,
            left=map_left,
            top=map_top,
            width=map_width,
            height=map_height,
            body_font=body_font,
        )

    roster_left = map_left + map_width + 38
    draw.text((roster_left, map_top), "INITIATIVE", fill=_INK, font=meta_font)
    row_top = map_top + 38
    for actor in sorted(combatants, key=lambda item: -int(item.get("initiative", 0) or 0)):
        actor_id = str(actor.get("actor_id") or "")
        current = actor_id == current_actor_id
        draw.rounded_rectangle(
            (roster_left, row_top, canvas_width - 42, row_top + 66),
            radius=10,
            fill="#f8dfd4" if current else "#fffaf0",
            outline=_ACCENT if current else "#d5cfc2",
            width=2 if current else 1,
        )
        _draw_round_portrait(
            canvas,
            draw,
            center=(roster_left + 36, row_top + 33),
            diameter=44,
            actor=actor,
            portrait=portrait_values.get(actor_id),
            current=current,
        )
        draw.text(
            (roster_left + 70, row_top + 9),
            str(int(actor.get("initiative", 0) or 0)),
            fill=_ACCENT,
            font=meta_font,
        )
        name = _fit_text(
            draw,
            str(actor.get("name") or actor_id),
            meta_font,
            roster_width - 145,
        )
        draw.text((roster_left + 112, row_top + 9), name, fill=_INK, font=meta_font)
        details = []
        position = actor.get("position")
        if isinstance(position, Mapping):
            details.append(f"({position.get('x')}, {position.get('y')})")
        hp = actor.get("hp")
        if isinstance(hp, Mapping) and hp.get("current") is not None:
            details.append(f"HP {hp.get('current')}/{hp.get('max', '?')}")
        details.extend(_condition_labels(actor.get("conditions"))[:3])
        detail_text = _fit_text(
            draw,
            " - ".join(details) or "VISIBLE COMBATANT",
            small_font,
            roster_width - 100,
        )
        draw.text((roster_left + 72, row_top + 40), detail_text, fill=_MUTED, font=small_font)
        row_top += 78

    map_revision = int(battle_map.get("map_revision", 1) or 1) if battle_map else 0
    grid_label = (
        f"MAP REV {map_revision} - {width_cells}x{height_cells} - "
        f"{int(dict(battle_map.get('grid') or {}).get('cell_ft', 5) or 5)} FT CELLS"
        if grid_mode
        else "AGENT MODE - IMAGE CONTAINS NO MECHANICAL GEOMETRY"
    )
    footer_y = canvas_height - 48
    draw.text((42, footer_y), grid_label, fill=_MUTED, font=small_font)
    draw.text(
        (canvas_width - 42, footer_y),
        "SAGASMITH - MCP STATE IS AUTHORITATIVE",
        fill=_MUTED,
        font=small_font,
        anchor="ra",
    )

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    content = output.getvalue()
    actor_summaries = []
    for actor in combatants:
        position = actor.get("position")
        location = (
            f" at {position.get('x')},{position.get('y')}"
            if isinstance(position, Mapping)
            else ""
        )
        actor_summaries.append(
            f"{actor.get('name') or actor.get('actor_id')}, initiative "
            f"{int(actor.get('initiative', 0) or 0)}{location}"
        )
    metadata = {
        "encounter_id": value.get("id"),
        "positioning_mode": positioning_mode,
        "round": int(value.get("round", 1) or 1),
        "current_actor_id": current_actor_id,
        "map_revision": map_revision if grid_mode else None,
        "audience_projection": audience_projection,
        "width": canvas_width,
        "height": canvas_height,
        "mime_type": "image/png",
        "image_checksum": hashlib.sha256(content).hexdigest(),
        "alt_text": (
            f"{value.get('name') or 'Combat'}, round {int(value.get('round', 1) or 1)}. "
            + "; ".join(actor_summaries)
        ),
    }
    return metadata, content


__all__ = ["render_combat_png"]

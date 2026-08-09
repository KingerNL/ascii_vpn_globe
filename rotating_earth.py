#!/usr/bin/env python3
"""An interactive, rotating Earth rendered with Unicode Braille cells."""

from __future__ import annotations

import argparse
from array import array
import base64
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import math
import multiprocessing
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import time
import tty
import zlib

from boundary_data import BORDER_LINES
from earth_texture import DATA, HEIGHT as TEXTURE_HEIGHT, WIDTH as TEXTURE_WIDTH
from nordvpn_cities import CITIES as NORDVPN_CITIES


BRAILLE = tuple(chr(0x2800 + value) for value in range(256))
BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)

# An ordered dither keeps ocean shading visible without sacrificing Braille resolution.
BAYER_8X8 = (
    (0, 48, 12, 60, 3, 51, 15, 63),
    (32, 16, 44, 28, 35, 19, 47, 31),
    (8, 56, 4, 52, 11, 59, 7, 55),
    (40, 24, 36, 20, 43, 27, 39, 23),
    (2, 50, 14, 62, 1, 49, 13, 61),
    (34, 18, 46, 30, 33, 17, 45, 29),
    (10, 58, 6, 54, 9, 57, 5, 53),
    (42, 26, 38, 22, 41, 25, 37, 21),
)

OCEAN = 0
LAND = 1
ICE = 2
RESET = "\x1b[0m"
CITY_COLOR = "\x1b[38;2;255;203;72m"
HOVER_CITY_COLOR = "\x1b[38;2;236;73;53m"
SELECTED_CITY_COLOR = "\x1b[38;2;255;247;196m"
CITY_LABEL_COLOR = "\x1b[38;2;255;230;145m"
DIALOG_COLOR = "\x1b[38;2;255;247;196m"
INTERNAL_BORDER_COLOR = "\x1b[38;2;226;181;103m"
STAR_COLOR = "\x1b[38;2;104;119;151m"
TAU = 2.0 * math.pi
MAX_PITCH = math.radians(82.0)
DEFAULT_YAW = math.radians(15.0)
DEFAULT_PITCH = math.radians(45.0)
POLAR_EDGE_BASE = math.radians(66.0)
POLAR_EDGE_VARIATION = math.radians(8.0)
POLAR_CAP_LATITUDE = math.radians(83.0)

_CPU_COUNT_FUNCTION = getattr(os, "process_cpu_count", os.cpu_count)
AVAILABLE_CPUS = _CPU_COUNT_FUNCTION() or 1
DEFAULT_WORKERS = min(4, max(1, AVAILABLE_CPUS // 4))


def _world_vector(longitude: float, latitude: float) -> tuple[float, float, float]:
    cos_latitude = math.cos(latitude)
    return (
        cos_latitude * math.sin(longitude),
        math.sin(latitude),
        cos_latitude * math.cos(longitude),
    )


NORDVPN_CITY_DATA = tuple(
    (
        identifier,
        name,
        country_code,
        math.radians(longitude),
        math.radians(latitude),
    )
    for identifier, name, country_code, latitude, longitude in NORDVPN_CITIES
)
NORDVPN_CITY_NAMES = {
    identifier: (name, country_code)
    for identifier, name, country_code, _, _ in NORDVPN_CITY_DATA
}
NORDVPN_CITY_VECTORS = tuple(
    (
        identifier,
        name,
        country_code,
        _world_vector(longitude, latitude),
    )
    for identifier, name, country_code, longitude, latitude in NORDVPN_CITY_DATA
)
BOUNDARY_LINES_RADIANS = tuple(
    (
        label,
        tuple(
            (math.radians(longitude), math.radians(latitude))
            for longitude, latitude in coordinates
        ),
    )
    for label, coordinates in BORDER_LINES
)
BOUNDARY_LINE_VECTORS = tuple(
    (
        label,
        tuple(_world_vector(longitude, latitude) for longitude, latitude in points),
    )
    for label, points in BOUNDARY_LINES_RADIANS
)

_LAND_BITS = zlib.decompress(base64.b85decode(DATA))
_EXPECTED_TEXTURE_BYTES = (TEXTURE_WIDTH * TEXTURE_HEIGHT + 7) // 8
if len(_LAND_BITS) != _EXPECTED_TEXTURE_BYTES:
    raise RuntimeError("The bundled Earth texture is corrupt")
_LAND_MASK = bytes(
    1 if _LAND_BITS[index >> 3] & (0x80 >> (index & 7)) else 0
    for index in range(TEXTURE_WIDTH * TEXTURE_HEIGHT)
)


def _build_terrain_texture() -> array:
    texture = array("f")
    for y in range(TEXTURE_HEIGHT):
        latitude = math.pi / 2.0 - (y + 0.5) / TEXTURE_HEIGHT * math.pi
        for x in range(TEXTURE_WIDTH):
            longitude = (x + 0.5) / TEXTURE_WIDTH * TAU - math.pi
            texture.append(
                0.50
                + 0.22 * math.sin(longitude * 11.0 + latitude * 7.0)
                + 0.16 * math.sin(longitude * 23.0 - latitude * 13.0)
            )
    return texture


_TERRAIN_TEXTURE = _build_terrain_texture()

_MOUSE_RE = re.compile(br"^\x1b\[<(\d+);(\d+);(\d+)([Mm])")


def _mix(
    dark: tuple[int, int, int],
    light: tuple[int, int, int],
    amount: float,
) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * amount) for a, b in zip(dark, light))


def _build_palette() -> dict[tuple[int, int, int], str]:
    endpoints = {
        OCEAN: ((8, 38, 91), (42, 158, 224)),
        LAND: ((45, 73, 36), (144, 185, 82)),
        ICE: ((118, 151, 171), (231, 244, 246)),
    }
    palette: dict[tuple[int, int, int], str] = {}
    for material, (dark, light) in endpoints.items():
        for light_level in range(7):
            for rim_level in range(3):
                color = _mix(dark, light, light_level / 6.0)
                if rim_level:
                    atmosphere = (80, 190, 245)
                    color = _mix(color, atmosphere, rim_level * 0.14)
                palette[material, light_level, rim_level] = (
                    f"\x1b[38;2;{color[0]};{color[1]};{color[2]}m"
                )
    return palette


COLOR_PALETTE = _build_palette()


def _texture_position(longitude: float, latitude: float) -> tuple[int, int]:
    x = int(((longitude + math.pi) % TAU) / TAU * TEXTURE_WIDTH)
    latitude = max(-math.pi / 2.0, min(math.pi / 2.0, latitude))
    y = int((math.pi / 2.0 - latitude) / math.pi * TEXTURE_HEIGHT)
    y = min(TEXTURE_HEIGHT - 1, y)
    return x, y


def is_land(longitude: float, latitude: float) -> bool:
    """Return whether a longitude/latitude pair in radians falls on land."""
    x, y = _texture_position(longitude, latitude)
    return bool(_LAND_MASK[y * TEXTURE_WIDTH + x])


def screen_to_geo(
    x: float, y: float, yaw: float, pitch: float
) -> tuple[float, float, float] | None:
    """Project a point on the visible unit disk into longitude and latitude."""
    return _screen_to_geo_rotated(
        x,
        y,
        math.sin(pitch),
        math.cos(pitch),
        math.sin(yaw),
        math.cos(yaw),
    )


def _screen_to_geo_rotated(
    x: float,
    y: float,
    sin_pitch: float,
    cos_pitch: float,
    sin_yaw: float,
    cos_yaw: float,
) -> tuple[float, float, float] | None:
    radius_squared = x * x + y * y
    if radius_squared > 1.0:
        return None

    z = math.sqrt(max(0.0, 1.0 - radius_squared))
    pitched_y = y * cos_pitch + z * sin_pitch
    pitched_z = -y * sin_pitch + z * cos_pitch

    world_x = x * cos_yaw + pitched_z * sin_yaw
    world_z = -x * sin_yaw + pitched_z * cos_yaw

    longitude = math.atan2(world_x, world_z)
    latitude = math.asin(max(-1.0, min(1.0, pitched_y)))
    return longitude, latitude, z


def geo_to_screen(
    longitude: float, latitude: float, yaw: float, pitch: float
) -> tuple[float, float, float]:
    """Project a geographic point into screen x, y, and visible depth."""
    return _geo_to_screen_rotated(
        longitude,
        latitude,
        math.sin(pitch),
        math.cos(pitch),
        math.sin(yaw),
        math.cos(yaw),
    )


def _geo_to_screen_rotated(
    longitude: float,
    latitude: float,
    sin_pitch: float,
    cos_pitch: float,
    sin_yaw: float,
    cos_yaw: float,
) -> tuple[float, float, float]:
    return _world_to_screen_rotated(
        *_world_vector(longitude, latitude),
        sin_pitch,
        cos_pitch,
        sin_yaw,
        cos_yaw,
    )


def _world_to_screen_rotated(
    world_x: float,
    world_y: float,
    world_z: float,
    sin_pitch: float,
    cos_pitch: float,
    sin_yaw: float,
    cos_yaw: float,
) -> tuple[float, float, float]:

    rotated_x = world_x * cos_yaw - world_z * sin_yaw
    rotated_z = world_x * sin_yaw + world_z * cos_yaw

    screen_y = world_y * cos_pitch - rotated_z * sin_pitch
    screen_z = world_y * sin_pitch + rotated_z * cos_pitch
    return rotated_x, screen_y, screen_z


@dataclass(frozen=True)
class ProjectedCity:
    identifier: str
    name: str
    country_code: str
    pixel_x: int
    pixel_y: int
    depth: float

    @property
    def cell(self) -> tuple[int, int]:
        return self.pixel_x // 2, self.pixel_y // 4


def globe_radius(columns: int, rows: int) -> float:
    """Return globe radius in Braille subpixels for a terminal area."""
    return max(1.0, min(columns * 2.0 * 0.44, rows * 4.0 * 0.46))


def project_cities(
    columns: int, rows: int, yaw: float, pitch: float
) -> tuple[ProjectedCity, ...]:
    """Project visible NordVPN cities into terminal subpixel coordinates."""
    pixel_width = columns * 2
    pixel_height = rows * 4
    center_x = pixel_width / 2.0
    center_y = pixel_height / 2.0
    radius = globe_radius(columns, rows)
    sin_pitch = math.sin(pitch)
    cos_pitch = math.cos(pitch)
    sin_yaw = math.sin(yaw)
    cos_yaw = math.cos(yaw)
    projected: list[ProjectedCity] = []

    for identifier, name, country_code, world in NORDVPN_CITY_VECTORS:
        screen_x, screen_y, depth = _world_to_screen_rotated(
            *world,
            sin_pitch,
            cos_pitch,
            sin_yaw,
            cos_yaw,
        )
        if depth <= 0.02:
            continue

        pixel_x = round(center_x + screen_x * radius - 0.5)
        pixel_y = round(center_y - screen_y * radius - 0.5)
        if not (0 <= pixel_x < pixel_width and 0 <= pixel_y < pixel_height):
            continue

        projected.append(
            ProjectedCity(
                identifier,
                name,
                country_code,
                pixel_x,
                pixel_y,
                depth,
            )
        )

    return tuple(projected)


def _city_marker_masks(
    projected_cities: tuple[ProjectedCity, ...],
) -> dict[tuple[int, int], int]:
    markers: dict[tuple[int, int], int] = {}
    for city in projected_cities:
        dot = BRAILLE_BITS[city.pixel_y % 4][city.pixel_x % 2]
        markers[city.cell] = markers.get(city.cell, 0) | dot
    return markers


def project_city_markers(
    columns: int, rows: int, yaw: float, pitch: float
) -> dict[tuple[int, int], int]:
    """Project visible NordVPN cities into Braille cell masks."""
    return _city_marker_masks(project_cities(columns, rows, yaw, pitch))


def pick_city(
    cell_x: int,
    cell_y: int,
    columns: int,
    rows: int,
    yaw: float,
    pitch: float,
) -> ProjectedCity | None:
    """Return the nearest visible city occupying a terminal cell."""
    if not (0 <= cell_x < columns and 0 <= cell_y < rows):
        return None

    click_x = cell_x * 2 + 1.0
    click_y = cell_y * 4 + 2.0
    closest: ProjectedCity | None = None
    closest_distance = math.inf
    for city in project_cities(columns, rows, yaw, pitch):
        if city.cell != (cell_x, cell_y):
            continue
        distance = (
            ((city.pixel_x - click_x) / 2.0) ** 2
            + ((city.pixel_y - click_y) / 4.0) ** 2
        )
        if distance < closest_distance:
            closest = city
            closest_distance = distance

    return closest


def project_internal_borders(
    columns: int,
    rows: int,
    yaw: float,
    pitch: float,
) -> dict[tuple[int, int], int]:
    """Rasterize requested international boundaries into Braille masks."""
    pixel_width = columns * 2
    pixel_height = rows * 4
    center_x = pixel_width / 2.0
    center_y = pixel_height / 2.0
    radius = globe_radius(columns, rows)
    markers: dict[tuple[int, int], int] = {}
    sin_pitch = math.sin(pitch)
    cos_pitch = math.cos(pitch)
    sin_yaw = math.sin(yaw)
    cos_yaw = math.cos(yaw)

    for _, coordinates in BOUNDARY_LINE_VECTORS:
        previous: tuple[float, float, float] | None = None
        for world in coordinates:
            current = _world_to_screen_rotated(
                *world,
                sin_pitch,
                cos_pitch,
                sin_yaw,
                cos_yaw,
            )
            if previous is not None and previous[2] > 0.01 and current[2] > 0.01:
                start_x = center_x + previous[0] * radius - 0.5
                start_y = center_y - previous[1] * radius - 0.5
                end_x = center_x + current[0] * radius - 0.5
                end_y = center_y - current[1] * radius - 0.5
                steps = max(
                    1,
                    math.ceil(max(abs(end_x - start_x), abs(end_y - start_y))),
                )
                for step in range(steps + 1):
                    amount = step / steps
                    pixel_x = round(start_x + (end_x - start_x) * amount)
                    pixel_y = round(start_y + (end_y - start_y) * amount)
                    if not (
                        0 <= pixel_x < pixel_width
                        and 0 <= pixel_y < pixel_height
                    ):
                        continue
                    cell = pixel_x // 2, pixel_y // 4
                    dot = BRAILLE_BITS[pixel_y % 4][pixel_x % 2]
                    markers[cell] = markers.get(cell, 0) | dot
            previous = current

    return markers


def _surface_sample(
    normal_x: float,
    normal_y: float,
    normal_z: float,
    longitude: float,
    latitude: float,
) -> tuple[int, float, float, float]:
    texture_x, texture_y = _texture_position(longitude, latitude)
    texture_index = texture_y * TEXTURE_WIDTH + texture_x
    terrain = _TERRAIN_TEXTURE[texture_index]
    land = bool(_LAND_MASK[texture_index])
    polar_edge = POLAR_EDGE_BASE + POLAR_EDGE_VARIATION * terrain
    if (land and abs(latitude) > polar_edge) or abs(latitude) > POLAR_CAP_LATITUDE:
        material = ICE
    elif land:
        material = LAND
    else:
        material = OCEAN

    # Fixed screen-space lighting makes the sphere readable as geography rotates.
    diffuse = max(
        0.0,
        normal_x * -0.42 + normal_y * 0.34 + normal_z * 0.84,
    )
    rim = (1.0 - normal_z) ** 2
    base_density = (0.25, 0.52, 0.68)[material]
    density = base_density + 0.36 * diffuse + 0.08 * rim + 0.035 * terrain
    density = max(0.06, min(0.98, density))
    light = 0.12 + 0.88 * diffuse
    return material, density, light, rim


def _star_mask(cell_x: int, cell_y: int) -> int:
    """Return a stable sparse Braille mask for one background cell."""
    value = ((cell_x + 11) * 0x045D9F3B) ^ ((cell_y + 17) * 0x119DE1F3)
    value &= 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x045D9F3B) & 0xFFFFFFFF
    value ^= value >> 16
    if value % 100 >= 3:
        return 0

    mask = 1 << ((value >> 8) & 7)
    if ((value >> 16) & 0x3F) == 0:
        mask |= 1 << ((value >> 20) & 7)
    return mask


def _city_label_cells(
    projected_cities: tuple[ProjectedCity, ...],
    selected_city: str | None,
    columns: int,
    rows: int,
) -> tuple[dict[tuple[int, int], str], tuple[int, int] | None]:
    if selected_city is None:
        return {}, None

    city = next(
        (
            candidate
            for candidate in projected_cities
            if candidate.identifier == selected_city
        ),
        None,
    )
    if city is None:
        return {}, None

    marker_x, marker_y = city.cell
    text = f"[{city.name}, {city.country_code}]"[:columns]
    right_start = marker_x + 2
    left_start = marker_x - len(text) - 1
    if right_start + len(text) <= columns:
        start_x = right_start
    elif left_start >= 0:
        start_x = left_start
    else:
        start_x = max(0, min(columns - len(text), marker_x - len(text) // 2))

    if marker_y > 0:
        label_y = marker_y - 1
    elif marker_y + 1 < rows:
        label_y = marker_y + 1
    else:
        label_y = marker_y

    cells = {
        (start_x + offset, label_y): character
        for offset, character in enumerate(text)
    }
    return cells, city.cell


def city_label_hit(
    cell_x: int,
    cell_y: int,
    columns: int,
    rows: int,
    yaw: float,
    pitch: float,
    selected_city: str | None,
) -> bool:
    """Return whether a terminal cell belongs to the selected city label."""
    if selected_city is None:
        return False
    projected_cities = project_cities(columns, rows, yaw, pitch)
    label_cells, _ = _city_label_cells(
        projected_cities,
        selected_city,
        columns,
        rows,
    )
    return (cell_x, cell_y) in label_cells


def _dialog_cells(
    columns: int,
    rows: int,
    confirm_city: str | None,
    message: str | None,
) -> dict[tuple[int, int], str]:
    if columns <= 0 or rows <= 0 or (confirm_city is None and message is None):
        return {}

    max_content_width = max(1, columns - 6)
    if confirm_city in NORDVPN_CITY_NAMES:
        name, country_code = NORDVPN_CITY_NAMES[confirm_city]
        content = [
            "NordVPN connection",
            f"Connect to {name}, {country_code}?",
            "y/Enter: connect   n: cancel",
        ]
    else:
        wrapped = textwrap.wrap(
            message or "NordVPN command finished.",
            width=max_content_width,
        ) or [""]
        content = ["NordVPN", *wrapped[: max(1, rows - 4)], "Enter: close"]

    inner_width = min(
        max_content_width,
        max(14, max(len(line) for line in content)),
    )
    content = [line[:inner_width] for line in content]
    box_width = inner_width + 2
    box_lines = [
        "+" + "-" * inner_width + "+",
        *(f"|{line.center(inner_width)}|" for line in content),
        "+" + "-" * inner_width + "+",
    ]
    box_lines = box_lines[:rows]
    start_x = max(0, (columns - box_width) // 2)
    start_y = max(0, (rows - len(box_lines)) // 2)
    return {
        (start_x + x, start_y + y): character
        for y, line in enumerate(box_lines)
        for x, character in enumerate(line[:columns])
        if start_x + x < columns
    }


def render_globe(
    columns: int,
    rows: int,
    yaw: float,
    pitch: float,
    color: bool = True,
    show_cities: bool = True,
    selected_city: str | None = None,
    hovered_city: str | None = None,
    hovered_label: bool = False,
    confirm_city: str | None = None,
    dialog_message: str | None = None,
    row_start: int = 0,
    row_end: int | None = None,
) -> list[str]:
    """Render a globe into rows of terminal cells."""
    if columns <= 0 or rows <= 0:
        return []
    row_start = max(0, row_start)
    row_end = rows if row_end is None else min(rows, row_end)
    if row_start >= row_end:
        return []

    pixel_width = columns * 2
    pixel_height = rows * 4
    center_x = pixel_width / 2.0
    center_y = pixel_height / 2.0
    radius = globe_radius(columns, rows)
    sin_pitch = math.sin(pitch)
    cos_pitch = math.cos(pitch)
    sin_yaw = math.sin(yaw)
    cos_yaw = math.cos(yaw)

    left = max(0, int((center_x - radius) // 2) - 1)
    right = min(columns - 1, int((center_x + radius) // 2) + 1)
    top = max(0, int((center_y - radius) // 4) - 1)
    bottom = min(rows - 1, int((center_y + radius) // 4) + 1)
    projected_cities = (
        project_cities(columns, rows, yaw, pitch) if show_cities else ()
    )
    city_markers = _city_marker_masks(projected_cities)
    internal_borders = project_internal_borders(columns, rows, yaw, pitch)
    label_cells, selected_cell = _city_label_cells(
        projected_cities,
        selected_city,
        columns,
        rows,
    )
    hovered_cell = next(
        (
            city.cell
            for city in projected_cities
            if city.identifier == hovered_city
        ),
        None,
    )
    dialog_cells = _dialog_cells(
        columns,
        rows,
        confirm_city,
        dialog_message,
    )

    output: list[str] = []
    for cell_y in range(row_start, row_end):
        pieces: list[str] = []
        active_color: str | None = None
        for cell_x in range(columns):
            dialog_character = dialog_cells.get((cell_x, cell_y))
            label_character = label_cells.get((cell_x, cell_y))
            city_marker = city_markers.get((cell_x, cell_y), 0)
            internal_border = internal_borders.get((cell_x, cell_y), 0)
            dots = 0
            inside = False
            material_counts = [0, 0, 0]
            light_total = 0.0
            rim_total = 0.0
            lit_dots = 0

            next_color: str | None = None
            character = " "
            if dialog_character is not None:
                character = dialog_character
                if color:
                    next_color = DIALOG_COLOR
            elif label_character is not None:
                character = label_character
                if color:
                    next_color = (
                        HOVER_CITY_COLOR if hovered_label else CITY_LABEL_COLOR
                    )
            else:
                if left <= cell_x <= right and top <= cell_y <= bottom:
                    for dot_y in range(4):
                        pixel_y = cell_y * 4 + dot_y
                        normal_y = (center_y - (pixel_y + 0.5)) / radius
                        for dot_x in range(2):
                            pixel_x = cell_x * 2 + dot_x
                            normal_x = ((pixel_x + 0.5) - center_x) / radius
                            projected = _screen_to_geo_rotated(
                                normal_x,
                                normal_y,
                                sin_pitch,
                                cos_pitch,
                                sin_yaw,
                                cos_yaw,
                            )
                            if projected is None:
                                continue

                            inside = True
                            longitude, latitude, normal_z = projected
                            material, density, light, rim = _surface_sample(
                                normal_x,
                                normal_y,
                                normal_z,
                                longitude,
                                latitude,
                            )
                            threshold = (
                                BAYER_8X8[pixel_y & 7][pixel_x & 7] + 0.5
                            ) / 64.0
                            if density <= threshold:
                                continue

                            dots |= BRAILLE_BITS[dot_y][dot_x]
                            material_counts[material] += 1
                            light_total += light
                            rim_total += rim
                            lit_dots += 1

                if city_marker:
                    inside = True
                    dots = dots | city_marker if color else 0xFF
                if internal_border:
                    inside = True
                    dots |= internal_border

                if inside:
                    character = BRAILLE[dots]
                    if color and city_marker:
                        if (cell_x, cell_y) == hovered_cell:
                            next_color = HOVER_CITY_COLOR
                        elif (cell_x, cell_y) == selected_cell:
                            next_color = SELECTED_CITY_COLOR
                        else:
                            next_color = CITY_COLOR
                    elif color and internal_border:
                        next_color = INTERNAL_BORDER_COLOR
                    elif color and lit_dots:
                        material = max(
                            range(3),
                            key=material_counts.__getitem__,
                        )
                        light_level = min(
                            6,
                            round(light_total / lit_dots * 6.0),
                        )
                        rim_level = min(
                            2,
                            round(rim_total / lit_dots * 2.0),
                        )
                        next_color = COLOR_PALETTE[
                            material,
                            light_level,
                            rim_level,
                        ]
                else:
                    star = _star_mask(cell_x, cell_y)
                    if star:
                        character = BRAILLE[star]
                        if color:
                            next_color = STAR_COLOR

            if next_color != active_color:
                if next_color is None:
                    if active_color is not None:
                        pieces.append(RESET)
                else:
                    pieces.append(next_color)
                active_color = next_color

            pieces.append(character)

        if active_color is not None:
            pieces.append(RESET)
        output.append("".join(pieces))

    return output


@dataclass(frozen=True)
class RenderStrip:
    columns: int
    rows: int
    yaw: float
    pitch: float
    color: bool
    show_cities: bool
    selected_city: str | None
    hovered_city: str | None
    hovered_label: bool
    confirm_city: str | None
    dialog_message: str | None
    row_start: int
    row_end: int


def _render_globe_strip(request: RenderStrip) -> list[str]:
    return render_globe(
        request.columns,
        request.rows,
        request.yaw,
        request.pitch,
        request.color,
        request.show_cities,
        request.selected_city,
        request.hovered_city,
        request.hovered_label,
        request.confirm_city,
        request.dialog_message,
        request.row_start,
        request.row_end,
    )


def _split_row_ranges(
    columns: int,
    rows: int,
    worker_count: int,
) -> list[tuple[int, int]]:
    """Split rows into contiguous strips with roughly equal sphere area."""
    worker_count = max(1, min(worker_count, rows))
    if worker_count == 1:
        return [(0, rows)]

    radius = globe_radius(columns, rows)
    center_y = rows * 2.0
    weights = []
    for row in range(rows):
        normal_y = (center_y - (row * 4.0 + 2.0)) / radius
        chord = math.sqrt(max(0.0, 1.0 - normal_y * normal_y)) * radius
        weights.append(1.0 + chord)

    ranges: list[tuple[int, int]] = []
    start = 0
    remaining_weight = sum(weights)
    for remaining_workers in range(worker_count, 1, -1):
        target = remaining_weight / remaining_workers
        end = start
        strip_weight = 0.0
        last_end = rows - (remaining_workers - 1)
        while end < last_end and (end == start or strip_weight < target):
            strip_weight += weights[end]
            end += 1
        ranges.append((start, end))
        start = end
        remaining_weight -= strip_weight
    ranges.append((start, rows))
    return ranges


def _worker_ready(worker: int) -> int:
    return worker


def _initialize_worker() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def connect_nordvpn(identifier: str) -> str:
    """Connect to one bundled NordVPN city and return a display message."""
    if identifier not in NORDVPN_CITY_NAMES:
        return "Unknown NordVPN city."

    name, country_code = NORDVPN_CITY_NAMES[identifier]
    city_argument = name.replace(" ", "_")
    command = [
        "nordvpn",
        "connect",
        country_code.lower(),
        city_argument,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=45.0,
            check=False,
        )
    except FileNotFoundError:
        return "NordVPN CLI was not found."
    except subprocess.TimeoutExpired:
        return f"Connection to {name} timed out."
    except OSError as error:
        return f"Could not start NordVPN: {error}"

    raw_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    clean_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw_output)
    ignored = {
        "A new version of NordVPN is available!",
        "Please update the app.",
    }
    lines = [
        line.strip()
        for line in clean_output.splitlines()
        if line.strip() and line.strip() not in ignored
    ]
    if lines:
        return lines[-1]
    if result.returncode == 0:
        return f"Connected to {name}, {country_code}."
    return f"NordVPN failed with exit code {result.returncode}."


class InputParser:
    """Incrementally parse keys and xterm SGR mouse reports."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[object, ...]]:
        self.buffer.extend(data)
        events: list[tuple[object, ...]] = []

        while self.buffer:
            if self.buffer[0] != 0x1B:
                byte = self.buffer.pop(0)
                events.append(("key", chr(byte)))
                continue

            if len(self.buffer) < 2:
                break
            if self.buffer[1] != ord("["):
                self.buffer.pop(0)
                events.append(("key", "escape"))
                continue
            if len(self.buffer) < 3:
                break

            if self.buffer[2] == ord("<"):
                match = _MOUSE_RE.match(self.buffer)
                if match is None:
                    if len(self.buffer) < 64 and not any(
                        byte in (ord("M"), ord("m")) for byte in self.buffer[3:]
                    ):
                        break
                    self.buffer.pop(0)
                    continue

                code, x, y = (int(group) for group in match.group(1, 2, 3))
                pressed = match.group(4) == b"M"
                events.append(("mouse", code, x, y, pressed))
                del self.buffer[: match.end()]
                continue

            final_index = next(
                (
                    index
                    for index, byte in enumerate(self.buffer[2:], start=2)
                    if 0x40 <= byte <= 0x7E
                ),
                None,
            )
            if final_index is None:
                break

            final = chr(self.buffer[final_index])
            if final in "ABCD":
                events.append(
                    (
                        "key",
                        {"A": "up", "B": "down", "C": "right", "D": "left"}[
                            final
                        ],
                    )
                )
            del self.buffer[: final_index + 1]

        return events

    def flush_escape(self) -> list[tuple[object, ...]]:
        """Resolve a lone Escape after the terminal has no more input ready."""
        if self.buffer == b"\x1b":
            self.buffer.clear()
            return [("key", "escape")]
        return []


@dataclass
class GlobeState:
    yaw: float = DEFAULT_YAW
    pitch: float = DEFAULT_PITCH
    speed: float = math.radians(6.0)
    paused: bool = False
    show_cities: bool = True
    selected_city: str | None = None
    hovered_city: str | None = None
    hovered_label: bool = False
    confirm_city: str | None = None
    pending_connect: str | None = None
    dialog_message: str | None = None
    dragging: bool = False
    drag_moved: bool = False
    pointer_known: bool = False
    mouse_x: int = 0
    mouse_y: int = 0

    def visual_signature(self) -> tuple[object, ...]:
        """Return only state that can change the rendered frame."""
        return (
            self.yaw,
            self.pitch,
            self.paused,
            self.show_cities,
            self.selected_city,
            self.hovered_city,
            self.hovered_label,
            self.confirm_city,
            self.dialog_message,
            self.dragging,
        )

    def animation_active(self) -> bool:
        return (
            not self.paused
            and not self.dragging
            and self.confirm_city is None
            and self.dialog_message is None
        )

    def refresh_hover(self, columns: int, rows: int) -> None:
        """Update the hovered marker after pointer or globe movement."""
        if (
            not self.show_cities
            or self.dragging
            or not self.pointer_known
            or self.confirm_city is not None
            or self.dialog_message is not None
        ):
            self.hovered_city = None
            self.hovered_label = False
            return

        globe_rows = max(1, rows - 1)
        self.hovered_label = city_label_hit(
            self.mouse_x - 1,
            self.mouse_y - 1,
            columns,
            globe_rows,
            self.yaw,
            self.pitch,
            self.selected_city,
        )
        if self.hovered_label:
            self.hovered_city = None
            return

        city = pick_city(
            self.mouse_x - 1,
            self.mouse_y - 1,
            columns,
            globe_rows,
            self.yaw,
            self.pitch,
        )
        self.hovered_city = city.identifier if city is not None else None

    def handle_event(
        self, event: tuple[object, ...], columns: int, rows: int
    ) -> bool:
        """Apply an input event and return False when the app should exit."""
        if event[0] == "key":
            key = event[1]
            if key in ("q", "Q", "\x03"):
                return False
            if self.confirm_city is not None:
                if key in ("y", "Y", "\r", "\n"):
                    self.pending_connect = self.confirm_city
                    name, country_code = NORDVPN_CITY_NAMES[self.confirm_city]
                    self.dialog_message = f"Connecting to {name}, {country_code}..."
                    self.confirm_city = None
                elif key in ("n", "N", "escape"):
                    self.confirm_city = None
                self.refresh_hover(columns, rows)
                return True
            if self.dialog_message is not None:
                if key in ("\r", "\n", " ", "n", "N", "escape"):
                    self.dialog_message = None
                self.refresh_hover(columns, rows)
                return True
            if key == "escape":
                return False
            if key == " ":
                self.paused = not self.paused
            elif key in ("n", "N"):
                self.show_cities = not self.show_cities
                if not self.show_cities:
                    self.selected_city = None
                    self.hovered_city = None
                    self.hovered_label = False
            elif key == "left":
                self.yaw -= math.radians(5.0)
            elif key == "right":
                self.yaw += math.radians(5.0)
            elif key == "up":
                self.pitch = min(MAX_PITCH, self.pitch + math.radians(5.0))
            elif key == "down":
                self.pitch = max(-MAX_PITCH, self.pitch - math.radians(5.0))
            elif key in ("r", "R"):
                self.yaw = DEFAULT_YAW
                self.pitch = DEFAULT_PITCH
                self.selected_city = None
            self.refresh_hover(columns, rows)
            return True

        if self.confirm_city is not None or self.dialog_message is not None:
            return True

        _, code, x, y, pressed = event
        code = int(code)
        x = int(x)
        y = int(y)
        pressed = bool(pressed)
        is_motion = bool(code & 32)
        button = code & 3

        if code & 64:
            self.mouse_x = x
            self.mouse_y = y
            self.pointer_known = True
            self.refresh_hover(columns, rows)
            return True

        if is_motion and button == 3:
            self.dragging = False
            self.mouse_x = x
            self.mouse_y = y
            self.pointer_known = True
            self.refresh_hover(columns, rows)
            return True

        if not pressed or (code & 3) == 3:
            if self.dragging and not self.drag_moved:
                globe_rows = max(1, rows - 1)
                clicked_label = self.show_cities and city_label_hit(
                    x - 1,
                    y - 1,
                    columns,
                    globe_rows,
                    self.yaw,
                    self.pitch,
                    self.selected_city,
                )
                if clicked_label:
                    self.confirm_city = self.selected_city
                else:
                    city = (
                        pick_city(
                            x - 1,
                            y - 1,
                            columns,
                            globe_rows,
                            self.yaw,
                            self.pitch,
                        )
                        if self.show_cities
                        else None
                    )
                    self.selected_city = (
                        city.identifier if city is not None else None
                    )
            self.dragging = False
            self.mouse_x = x
            self.mouse_y = y
            self.pointer_known = True
            self.refresh_hover(columns, rows)
            return True

        if not is_motion and (code & 3) == 0:
            self.dragging = True
            self.drag_moved = False
            self.pointer_known = True
            self.hovered_city = None
            self.hovered_label = False
            self.mouse_x = x
            self.mouse_y = y
            return True

        if is_motion and self.dragging:
            radius = globe_radius(columns, max(1, rows - 1))
            delta_x = x - self.mouse_x
            delta_y = y - self.mouse_y
            if delta_x or delta_y:
                self.drag_moved = True
            self.hovered_city = None
            self.hovered_label = False
            self.yaw = (self.yaw - delta_x * 2.0 / radius) % TAU
            self.pitch = max(
                -MAX_PITCH,
                min(MAX_PITCH, self.pitch + delta_y * 4.0 / radius),
            )
            self.mouse_x = x
            self.mouse_y = y
        return True


class ParallelRenderer:
    """Render horizontal globe strips in persistent worker processes."""

    def __init__(self, worker_count: int) -> None:
        self.worker_count = max(1, worker_count)
        self.executor: ProcessPoolExecutor | None = None

    def __enter__(self) -> ParallelRenderer:
        if self.worker_count == 1:
            return self
        try:
            sys.stdout.flush()
            try:
                context = multiprocessing.get_context("fork")
            except ValueError:
                context = multiprocessing.get_context()
            self.executor = ProcessPoolExecutor(
                max_workers=self.worker_count,
                mp_context=context,
                initializer=_initialize_worker,
            )
            list(
                self.executor.map(
                    _worker_ready,
                    range(self.worker_count),
                    chunksize=1,
                )
            )
        except Exception:
            self.close()
            self.worker_count = 1
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None

    def render(
        self,
        state: GlobeState,
        columns: int,
        rows: int,
        color: bool,
    ) -> list[str]:
        if self.executor is None or self.worker_count == 1:
            return render_globe(
                columns,
                rows,
                state.yaw,
                state.pitch,
                color,
                state.show_cities,
                state.selected_city,
                state.hovered_city,
                state.hovered_label,
                state.confirm_city,
                state.dialog_message,
            )

        requests = [
            RenderStrip(
                columns,
                rows,
                state.yaw,
                state.pitch,
                color,
                state.show_cities,
                state.selected_city,
                state.hovered_city,
                state.hovered_label,
                state.confirm_city,
                state.dialog_message,
                row_start,
                row_end,
            )
            for row_start, row_end in _split_row_ranges(
                columns,
                rows,
                self.worker_count,
            )
        ]
        try:
            chunks = list(
                self.executor.map(
                    _render_globe_strip,
                    requests,
                    chunksize=1,
                )
            )
        except Exception:
            self.close()
            self.worker_count = 1
            return self.render(state, columns, rows, color)
        return [line for chunk in chunks for line in chunk]


class TerminalSession:
    """Set terminal modes for interactive drawing and always restore them."""

    ENTER = "\x1b[?1049h\x1b[?25l\x1b[?1003h\x1b[?1006h\x1b[2J\x1b[H"
    EXIT = (
        "\x1b[0m\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l"
        "\x1b[?25h\x1b[?1049l"
    )

    def __init__(self) -> None:
        self.input_fd = sys.stdin.fileno()
        self.previous_settings: list[object] | None = None

    def __enter__(self) -> TerminalSession:
        self.previous_settings = termios.tcgetattr(self.input_fd)
        tty.setcbreak(self.input_fd)
        sys.stdout.write(self.ENTER)
        sys.stdout.flush()
        return self

    def __exit__(self, *_: object) -> None:
        try:
            sys.stdout.write(self.EXIT)
            sys.stdout.flush()
            termios.tcdrain(sys.stdout.fileno())
        finally:
            if self.previous_settings is not None:
                try:
                    termios.tcflush(self.input_fd, termios.TCIFLUSH)
                finally:
                    termios.tcsetattr(
                        self.input_fd,
                        termios.TCSAFLUSH,
                        self.previous_settings,
                    )


def _status_line(state: GlobeState, columns: int, color: bool) -> str:
    mode = "paused" if state.paused else f"{math.degrees(state.speed):.1f} deg/s"
    cities = "on" if state.show_cities else "off"
    selected = ""
    if state.selected_city in NORDVPN_CITY_NAMES:
        name, country_code = NORDVPN_CITY_NAMES[state.selected_city]
        selected = f" | {name}, {country_code}"
    text = (
        f" {mode} | Nord:{len(NORDVPN_CITIES)} {cities} (n){selected} | "
        "click city | drag | space | r | q "
    )
    text = text[:columns].center(columns)
    return f"\x1b[2m{text}{RESET}" if color else text


def _draw_frame(
    state: GlobeState,
    columns: int,
    rows: int,
    color: bool,
    renderer: ParallelRenderer | None = None,
) -> str:
    if columns < 16 or rows < 5:
        message = "Terminal too small"[:columns].center(columns)
        lines = [" " * columns for _ in range(max(0, rows - 1))]
        lines.append(message)
    else:
        if renderer is None:
            lines = render_globe(
                columns,
                rows - 1,
                state.yaw,
                state.pitch,
                color,
                state.show_cities,
                state.selected_city,
                state.hovered_city,
                state.hovered_label,
                state.confirm_city,
                state.dialog_message,
            )
        else:
            lines = renderer.render(state, columns, rows - 1, color)
        lines.append(_status_line(state, columns, color))
    return "\x1b[H" + "\r\n".join(lines)


def _advance_animation(
    state: GlobeState,
    elapsed: float,
    was_animation_active: bool,
) -> bool:
    """Advance an already-running animation without jumping on resume."""
    if not state.animation_active():
        return False
    if was_animation_active:
        state.yaw = (state.yaw + state.speed * elapsed) % TAU
    return True


def run(
    fps: float,
    speed: float,
    color: bool,
    show_cities: bool = True,
    workers: int = DEFAULT_WORKERS,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("rotating_earth.py needs an interactive terminal", file=sys.stderr)
        return 1

    state = GlobeState(
        speed=math.radians(speed),
        show_cities=show_cities,
    )
    parser = InputParser()
    frame_period = 1.0 / fps
    last_tick = time.monotonic()
    next_frame = last_tick
    running = True
    dirty = True
    terminal_size = shutil.get_terminal_size((80, 24))
    renderer = ParallelRenderer(workers)
    renderer.__enter__()

    old_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_on_sigterm(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        with TerminalSession():
            while running:
                now = time.monotonic()
                was_animation_active = state.animation_active()
                size = shutil.get_terminal_size((80, 24))
                if size != terminal_size:
                    terminal_size = size
                    dirty = True
                timeout = (
                    max(0.0, next_frame - now)
                    if dirty or state.animation_active()
                    else 0.25
                )
                readable, _, _ = select.select([sys.stdin], [], [], timeout)
                if readable:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        break
                    before = state.visual_signature()
                    for event in parser.feed(data):
                        running = state.handle_event(event, size.columns, size.lines)
                        if not running:
                            break
                    dirty = dirty or before != state.visual_signature()
                elif parser.buffer:
                    before = state.visual_signature()
                    for event in parser.flush_escape():
                        running = state.handle_event(event, size.columns, size.lines)
                    dirty = dirty or before != state.visual_signature()

                if not running:
                    break

                size = shutil.get_terminal_size((80, 24))
                if size != terminal_size:
                    terminal_size = size
                    dirty = True

                if running and state.pending_connect is not None:
                    identifier = state.pending_connect
                    state.pending_connect = None
                    sys.stdout.write(
                        _draw_frame(
                            state,
                            size.columns,
                            size.lines,
                            color,
                            renderer,
                        )
                    )
                    sys.stdout.flush()
                    state.dialog_message = connect_nordvpn(identifier)
                    dirty = True
                    next_frame = 0.0

                now = time.monotonic()
                elapsed = min(0.25, now - last_tick)
                last_tick = now
                if _advance_animation(state, elapsed, was_animation_active):
                    dirty = True

                if dirty and now >= next_frame:
                    state.refresh_hover(size.columns, size.lines)
                    sys.stdout.write(
                        _draw_frame(
                            state,
                            size.columns,
                            size.lines,
                            color,
                            renderer,
                        )
                    )
                    sys.stdout.flush()
                    dirty = False
                    next_frame = now + frame_period
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        renderer.close()

    return 0


def _bounded_fps(value: str) -> float:
    fps = float(value)
    if not 1.0 <= fps <= 60.0:
        raise argparse.ArgumentTypeError("FPS must be between 1 and 60")
    return fps


def _bounded_workers(value: str) -> int:
    workers = int(value)
    if not 1 <= workers <= AVAILABLE_CPUS:
        raise argparse.ArgumentTypeError(
            f"workers must be between 1 and {AVAILABLE_CPUS}"
        )
    return workers


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an interactive rotating Earth with Unicode Braille cells."
    )
    parser.add_argument(
        "--fps",
        type=_bounded_fps,
        default=24.0,
        help="rendering frame rate, from 1 to 60 (default: 24)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=6.0,
        metavar="DEGREES",
        help="automatic rotation in degrees per second (default: 6)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable 24-bit ANSI color",
    )
    parser.add_argument(
        "--no-cities",
        action="store_true",
        help="hide NordVPN city markers at startup",
    )
    parser.add_argument(
        "--workers",
        type=_bounded_workers,
        default=DEFAULT_WORKERS,
        metavar="COUNT",
        help=f"render worker processes (default: {DEFAULT_WORKERS})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(
        args.fps,
        args.speed,
        not args.no_color,
        not args.no_cities,
        args.workers,
    )


if __name__ == "__main__":
    raise SystemExit(main())

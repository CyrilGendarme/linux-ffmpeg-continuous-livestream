"""Faithful Python replica of the OBS browser overlays used in the MAIN_RADIO scene.

  * submodules/obscustomsetup/now_playing.html   -> right-hand panel (RGBA, 256x1080)
  * submodules/obscustomsetup/typewriter_radio.html -> centre "decoding" title (alpha mask)

Layout values come from the CSS (style.css, style_now_playing.css,
style_typewriter.css) and were checked against Chromium rendering the real
pages. The animations are line-by-line ports of textAnimation.js,
typewriter_common.js and the now_playing.html script, including their timer
semantics and random delays, driven by a deterministic clock that advances one
video frame at a time.
"""

import functools
import heapq
import itertools
import math
import random
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, features

from .constants import REPO_ROOT

OBS_DIR = REPO_ROOT / "submodules" / "obscustomsetup"
FALLBACK_COVER = OBS_DIR / "assets" / "icon_2_popuri.jpg"

CANVAS_W, CANVAS_H = 1920, 1080
PANEL_W = 256  # #now-playing-container-wrapper width
PANEL_X = CANVAS_W - PANEL_W
CENTER_W = CANVAS_W - PANEL_W  # .word-radio-wrapper: calc(100% - 256px)
CENTER_Y, CENTER_H = 140, 800  # region sent to FFmpeg for the centre title (8 lines of 100px)

WHITE = (255, 255, 255, 255)
RED = (0xC3, 0x15, 0x30, 255)  # .red-text
BOX = (0x11, 0x11, 0x11, 255)  # background-color: #111

CURSOR = "|"
HIDDEN_CURSOR = " "  # NBSP


# ---------------------------------------------------------------- text layout


@dataclass(frozen=True)
class TextStyle:
    font_file: str
    size: int
    letter_spacing: float
    line_height: float  # px
    optical_size: Optional[float] = None  # DMSans is variable: Chrome sets opsz = font-size

    def font(self) -> ImageFont.FreeTypeFont:
        return _font(self.font_file, self.size, self.optical_size)

    @property
    def ascent(self) -> int:
        return _metrics(self.font_file, self.size)[0]

    @property
    def descent(self) -> int:
        return _metrics(self.font_file, self.size)[1]

    def baseline_offset(self) -> int:
        """Distance from the top of a line box to the baseline (CSS half-leading model)."""
        content = self.ascent + self.descent
        return math.floor((self.line_height - content) / 2) + self.ascent


_FONT_CACHE: Dict[tuple, ImageFont.FreeTypeFont] = {}


def _font(path: str, size: int, opsz: Optional[float]) -> ImageFont.FreeTypeFont:
    key = (path, size, opsz)
    if key not in _FONT_CACHE:
        # Pillow >= 9 removed ImageFont.Layout; older versions used it for RAQM/BASIC layout selection.
        if hasattr(ImageFont, "Layout"):
            # RAQM (HarfBuzz) shapes text like Chrome; BASIC lacks GPOS kerning (sub-pixel differences).
            layout = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
            font = ImageFont.truetype(path, size, layout_engine=layout)
        else:
            font = ImageFont.truetype(path, size)
        if opsz is not None:
            axes = font.get_variation_axes()
            font.set_variation_by_axes(
                [opsz if axis["name"] in (b"Optical size", "Optical size") else axis["default"] for axis in axes]
            )
        _FONT_CACHE[key] = font
    return _FONT_CACHE[key]


def _metrics(path: str, size: int) -> Tuple[int, int]:
    """Chrome rounds hhea ascent/descent separately (all fonts here set USE_TYPO_METRICS = hhea)."""
    import struct

    data = Path(path).read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for index in range(count):
        tag, _checksum, offset, length = struct.unpack(">4sIII", data[12 + 16 * index : 28 + 16 * index])
        tables[tag] = data[offset : offset + length]
    units = struct.unpack(">H", tables[b"head"][18:20])[0]
    ascent, descent = struct.unpack(">hh", tables[b"hhea"][4:8])
    return round(ascent * size / units), round(-descent * size / units)


def _font_path(name: str) -> str:
    return str(OBS_DIR / "fonts" / name)


# style.css / style_now_playing.css / style_typewriter.css
PANEL_TITLE = TextStyle(_font_path("Staatliches-Regular.ttf"), 32, 4, 40)  # line-height normal = 1.25em
INFO_TEXT = TextStyle(_font_path("DMSans.ttf"), 20, 2, 24, optical_size=20)  # line-height 1.2
WORD = TextStyle(_font_path("Staatliches-Regular.ttf"), 80, 12, 100)


def collapse_whitespace(text: str) -> str:
    """CSS `white-space: normal`: collapse runs of spaces/newlines (NBSP is kept) and trim."""
    return re.sub(r"[ \t\n\r\f]+", " ", text).strip(" ")


def _width(style: TextStyle, text: str) -> float:
    return style.font().getlength(text) + len(text) * style.letter_spacing if text else 0.0


def _break_points(text: str) -> List[int]:
    """Indices after which a line may break: after spaces, and after a hyphen inside a word."""
    points = []
    for index, char in enumerate(text):
        if char == " ":
            points.append(index + 1)
        elif char == "-" and 0 < index < len(text) - 1 and text[index - 1] not in " " and not text[index + 1].isdigit() and text[index + 1] != " ":
            points.append(index + 1)
    return points


def wrap(style: TextStyle, text: str, max_width: float) -> List[str]:
    """Greedy line breaking like Chrome: trailing spaces hang, unbreakable words overflow."""
    text = collapse_whitespace(text)
    if not text:
        return []
    lines: List[str] = []
    start = 0
    while start < len(text):
        candidates = [point for point in _break_points(text) if point > start] + [len(text)]
        end = candidates[0]
        for point in candidates[1:]:
            if _width(style, text[start:point].rstrip(" ")) <= max_width + 0.01:
                end = point
            else:
                break
        if _width(style, text[start:len(text)].rstrip(" ")) <= max_width + 0.01:
            end = len(text)
        lines.append(text[start:end].rstrip(" "))
        start = end
    return lines


def draw_line(
    draw: ImageDraw.ImageDraw,
    style: TextStyle,
    x: float,
    line_top: float,
    text: str,
    colors: Optional[List[tuple]] = None,
    fill=WHITE,
) -> None:
    """Draw one line glyph by glyph so letter-spacing matches CSS (spacing after every glyph)."""
    font = style.font()
    baseline = round(line_top) + style.baseline_offset()
    for index, (char, offset) in enumerate(zip(text, _glyph_offsets(style, text))):
        if char in (" ", HIDDEN_CURSOR):
            continue
        draw.text((x + offset, baseline), char, font=font, fill=colors[index] if colors else fill, anchor="ls")


@functools.lru_cache(maxsize=4096)
def _glyph_offsets(style: TextStyle, text: str) -> Tuple[float, ...]:
    """x of each glyph: shaped width up to and including it, minus its own advance (keeps
    kerning with the previous glyph, e.g. "VA"), plus letter-spacing after each glyph."""
    font = style.font()
    return tuple(
        font.getlength(text[: index + 1]) - font.getlength(char) + index * style.letter_spacing
        for index, char in enumerate(text)
    )


# ------------------------------------------------------------ JS-like timers


class Scheduler:
    """setTimeout/setInterval on a manually advanced clock (milliseconds)."""

    def __init__(self, seed: Optional[int] = None) -> None:
        self.now = 0.0
        self.rng = random.Random(seed)  # all animation randomness: replayable from a seed
        self._queue: list = []
        self._ids = itertools.count(1)
        self._cancelled: set = set()

    def set_timeout(self, delay: float, callback: Callable[[], None]) -> int:
        timer_id = next(self._ids)
        heapq.heappush(self._queue, (self.now + max(0.0, delay), timer_id, callback, None))
        return timer_id

    def set_interval(self, period: float, callback: Callable[[], None]) -> int:
        timer_id = next(self._ids)
        heapq.heappush(self._queue, (self.now + period, timer_id, callback, period))
        return timer_id

    def clear(self, timer_id: Optional[int]) -> None:
        if timer_id is not None:
            self._cancelled.add(timer_id)

    def idle(self) -> bool:
        """True when no timer is pending (every animation has finished)."""
        return all(entry[1] in self._cancelled for entry in self._queue)

    def advance_to(self, time_ms: float) -> None:
        while self._queue and self._queue[0][0] <= time_ms:
            due, timer_id, callback, period = heapq.heappop(self._queue)
            if timer_id in self._cancelled:
                self._cancelled.discard(timer_id)
                continue
            self.now = due
            if period is not None:
                heapq.heappush(self._queue, (due + period, timer_id, callback, period))
            callback()
        self.now = time_ms


def random_delay(rng: random.Random, base: float, randomness: float = 0.2) -> float:
    """helpers.js randomDelay()."""
    variation = base * randomness
    return base + (rng.random() * variation * 2 - variation)


class TaskQueue:
    """Sequential promise chain (`queue = queue.then(...)`)."""

    def __init__(self) -> None:
        self._tasks: List[Callable[[Callable[[], None]], None]] = []
        self._running = False

    def then(self, task: Callable[[Callable[[], None]], None]) -> None:
        self._tasks.append(task)
        if not self._running:
            self._run_next()

    def _run_next(self) -> None:
        if not self._tasks:
            self._running = False
            return
        self._running = True
        self._tasks.pop(0)(self._run_next)


# --------------------------------------------------------- overlay elements


class Element:
    def __init__(self, text: str = "") -> None:
        self.text = text


def type_text(sched: Scheduler, element: Element, text: str, typing: float, wait: float, done) -> None:
    """textAnimation.js typeText()."""
    state = {"index": 0, "cursor": True}

    def blink() -> None:
        state["cursor"] = not state["cursor"]
        element.text = text[: state["index"]] + (CURSOR if state["cursor"] else HIDDEN_CURSOR)

    cursor_timer = sched.set_interval(500, blink)
    element.text = "|"

    def next_letter() -> None:
        if state["index"] < len(text):
            state["index"] += 1
            element.text = text[: state["index"]] + (CURSOR if state["cursor"] else HIDDEN_CURSOR)
            sched.set_timeout(random_delay(sched.rng, typing), next_letter)
        else:
            sched.clear(cursor_timer)
            element.text = text
            done()

    sched.set_timeout(random_delay(sched.rng, wait), next_letter)


def erase_text(sched: Scheduler, element: Element, typing: float, wait: float, keep_placeholder: bool, done) -> None:
    """textAnimation.js eraseText()."""
    text = element.text
    state = {"index": len(text), "cursor": True}

    def blink() -> None:
        state["cursor"] = not state["cursor"]
        element.text = text[: state["index"]] + (CURSOR if state["cursor"] else HIDDEN_CURSOR)

    cursor_timer = sched.set_interval(500, blink)
    element.text = text + CURSOR

    def backspace() -> None:
        if state["index"] > 0:
            state["index"] -= 1
            element.text = text[: state["index"]] + (CURSOR if state["cursor"] else HIDDEN_CURSOR)
            sched.set_timeout(random_delay(sched.rng, typing), backspace)
        else:
            sched.clear(cursor_timer)
            element.text = HIDDEN_CURSOR if keep_placeholder else ""
            done()

    sched.set_timeout(random_delay(sched.rng, wait), backspace)


FIELD_ORDER = ["artist", "title", "album", "label", "year", "bpm"]
FIELD_LABELS = {"artist": "Artist:", "title": "Title:", "album": "Album:", "label": "Label:", "year": "Year:", "bpm": "BPM:"}


def is_meaningful(value) -> bool:
    """helpers.js isMeaningfulValue()."""
    if value is None:
        return False
    normalized = str(value).strip()
    return bool(normalized) and normalized.lower() not in ("none", "null")


class NowPlayingPanel:
    """now_playing.html: menus, Now Playing fields with typing animation, cover with fade."""

    COVER_FADE_OUT_MS = 220
    COVER_FADE_IN_MS = 350

    def __init__(self, sched: Scheduler) -> None:
        self.sched = sched
        self.fields = {key: Element("") for key in FIELD_ORDER}
        self.animation_queue = TaskQueue()
        self.cover_queue = TaskQueue()
        self.cover_image = _load_cover(None)
        self.cover_token = "fallback"  # identifies cover_image in cache keys
        self.cover_background = False  # set to #111 on the first refresh
        self._opacity = (1.0, 1.0, 0.0, 0.0)  # from, to, start_ms, duration_ms

    # -- cover (refreshAlbumCover) ------------------------------------------
    def cover_opacity(self) -> float:
        start_value, end_value, start, duration = self._opacity
        if duration <= 0 or self.sched.now >= start + duration:
            return end_value
        progress = max(0.0, (self.sched.now - start) / duration)
        return start_value + (end_value - start_value) * _css_ease(progress)

    def _transition(self, target: float, duration: float) -> None:
        self._opacity = (self.cover_opacity(), target, self.sched.now, duration)

    def refresh_cover(self, cover: Optional[Image.Image], token: str = "") -> None:
        def task(done) -> None:
            self.cover_background = True
            self._transition(0.0, self.COVER_FADE_OUT_MS)

            def swap() -> None:
                self.cover_image = _load_cover(cover)
                self.cover_token = token if cover is not None and token else "fallback"
                self._transition(1.0, self.COVER_FADE_IN_MS)
                self.sched.set_timeout(self.COVER_FADE_IN_MS, done)

            self.sched.set_timeout(self.COVER_FADE_OUT_MS, swap)

        self.cover_queue.then(task)

    # -- fields (updateNowPlayingInfo) --------------------------------------
    def update(self, data: Dict[str, str]) -> None:
        normalized = dict(data)
        if normalized.get("year") == "0":
            normalized["year"] = ""
        targets = {}
        changed = False
        for key in FIELD_ORDER:
            current = self.fields[key].text.strip()
            target = normalized.get(key) if is_meaningful(normalized.get(key)) else HIDDEN_CURSOR
            targets[key] = target
            if target != current:
                changed = True
        if not changed:
            return

        def task(done) -> None:
            erase_order = list(reversed(FIELD_ORDER))

            def erase_next(index: int = 0) -> None:
                if index < len(erase_order):
                    erase_text(self.sched, self.fields[erase_order[index]], 80, 1000, True, lambda: erase_next(index + 1))
                else:
                    type_next(0)

            def type_next(index: int) -> None:
                if index < len(FIELD_ORDER):
                    key = FIELD_ORDER[index]
                    type_text(self.sched, self.fields[key], targets[key] or "", 80, 1800, lambda: type_next(index + 1))
                else:
                    done()

            erase_next()

        self.animation_queue.then(task)

    # -- rendering ----------------------------------------------------------
    def render(self) -> Image.Image:
        image = Image.new("RGBA", (PANEL_W, CANVAS_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        content_w = PANEL_W - 24 - 2 * 12  # margin-right 24, padding 12 + 12

        def block(y: float, title_runs, body_lines: List[str]) -> float:
            """A .menu-container: padding 8/12, Staatliches title then DMSans lines. Returns height."""
            title_lines = _wrap_runs(PANEL_TITLE, title_runs, content_w)
            height = 8 + len(title_lines) * PANEL_TITLE.line_height + len(body_lines) * INFO_TEXT.line_height + 8
            draw.rectangle([0, round(y), PANEL_W - 24 - 1, round(y + height) - 1], fill=BOX)
            line_y = y + 8
            for text, colors in title_lines:
                draw_line(draw, PANEL_TITLE, 12, line_y, text, colors)
                line_y += PANEL_TITLE.line_height
            for text in body_lines:
                draw_line(draw, INFO_TEXT, 12, line_y, text)
                line_y += INFO_TEXT.line_height
            return height

        # Today's menu
        y = 24.0
        y += block(y, [("Today's menu", WHITE)], ["45 to 33 rpm", "popuri music"]) + 12
        # Like Share Follow Love
        y += 24
        y += block(y, [("Like ", WHITE), ("Share ", RED), ("Follow ", WHITE), ("Love", RED)], []) + 12

        # Now Playing (flex-grow: 1) and the cover (230px, may shrink).
        info_y = y + 12
        title_lines = _wrap_runs(PANEL_TITLE, [("Now Playing", WHITE)], content_w)
        title_h = len(title_lines) * PANEL_TITLE.line_height
        value_lines = {key: wrap(INFO_TEXT, self.fields[key].text, content_w) for key in FIELD_ORDER}
        label_lines = {key: wrap(INFO_TEXT, FIELD_LABELS[key], content_w) for key in FIELD_ORDER}
        item_heights = [
            (len(label_lines[key]) + len(value_lines[key])) * INFO_TEXT.line_height for key in FIELD_ORDER
        ]
        content_base = 24 + sum(item_heights)
        info_base = 8 + title_h + content_base + 8
        cover_margin_top, cover_margin_bottom = 12, 24
        free = CANVAS_H - (info_y + info_base + 12 + cover_margin_top + 230 + cover_margin_bottom)
        info_h = info_base + max(0.0, free)
        cover_h = max(0.0, 230 + min(0.0, free))

        draw.rectangle([0, round(info_y), PANEL_W - 24 - 1, round(info_y + info_h) - 1], fill=BOX)
        line_y = info_y + 8
        for text, colors in title_lines:
            draw_line(draw, PANEL_TITLE, 12, line_y, text, colors)
            line_y += PANEL_TITLE.line_height
        content_h = info_h - 16 - title_h
        gap = max(0.0, (content_h - 24 - sum(item_heights)) / (len(FIELD_ORDER) - 1))
        item_y = line_y + 12
        for key, item_h in zip(FIELD_ORDER, item_heights):
            text_y = item_y
            for text in label_lines[key] + value_lines[key]:
                draw_line(draw, INFO_TEXT, 12, text_y, text)
                text_y += INFO_TEXT.line_height
            item_y += item_h + gap

        cover_y = info_y + info_h + 12 + cover_margin_top
        if cover_h >= 1:
            top, height = round(cover_y), round(cover_h)
            if self.cover_background:
                draw.rectangle([0, top, 229, top + height - 1], fill=BOX)
            opacity = self.cover_opacity()
            if opacity > 0:
                cover = self.cover_image.resize((230, height), Image.BILINEAR)  # object-fit: fill
                if opacity < 1:
                    alpha = cover.getchannel("A").point(lambda value: round(value * opacity))
                    cover.putalpha(alpha)
                image.alpha_composite(cover, (0, top))
        return image


def _wrap_runs(style: TextStyle, runs: List[Tuple[str, tuple]], max_width: float) -> List[Tuple[str, List[tuple]]]:
    """Wrap coloured inline runs (e.g. <span class="red-text">) and keep per-character colours."""
    text = ""
    colors: List[tuple] = []
    for run_text, color in runs:
        text += run_text
        colors += [color] * len(run_text)
    collapsed = collapse_whitespace(text)  # runs here contain single spaces already
    offset = len(text) - len(text.lstrip(" "))
    result = []
    position = offset
    for line in wrap(style, collapsed, max_width):
        start = text.index(line, position)
        result.append((line, colors[start : start + len(line)]))
        position = start + len(line)
    return result


def _css_ease(progress: float) -> float:
    """CSS `ease` = cubic-bezier(0.25, 0.1, 0.25, 1)."""
    x1, y1, x2, y2 = 0.25, 0.1, 0.25, 1.0

    def bezier(t: float, a: float, b: float) -> float:
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t**2 + t**3

    low, high = 0.0, 1.0
    for _ in range(40):
        mid = (low + high) / 2
        if bezier(mid, x1, x2) < progress:
            low = mid
        else:
            high = mid
    return bezier((low + high) / 2, y1, y2)


def _load_cover(cover: Optional[Image.Image]) -> Image.Image:
    if cover is None:
        cover = Image.open(FALLBACK_COVER)
    return cover.convert("RGBA")


class TypewriterTitle:
    """typewriter_common.js initWordDisplay(): centre-out 'decoding' reveal, cleared after 7.5s."""

    FRAME_DELAY = 110
    RANDOM_UNDERSCORE_CHANCE = 0.45
    CLEAR_AFTER_MS = 7500

    def __init__(self, sched: Scheduler) -> None:
        self.sched = sched
        self.element = Element("")
        self.final_word = ""
        self.radius = 0.0
        self.solved: List[bool] = []
        self.clear_timer: Optional[int] = None
        self._step()  # "optional initial animation"

    def _schedule_clear(self) -> None:
        if self.clear_timer is not None:
            self.sched.clear(self.clear_timer)

        def clear() -> None:
            self.element.text = ""
            self.final_word = ""
            self.solved = []
            self.radius = 0.0
            self.clear_timer = None

        self.clear_timer = self.sched.set_timeout(self.CLEAR_AFTER_MS, clear)

    def show(self, word: str) -> None:
        """resetAnimation()."""
        self.final_word = word.upper()
        self.radius = 0.0
        self.solved = [False] * len(self.final_word)
        self.element.text = ""
        self._schedule_clear()
        self._step()

    def _render(self) -> None:
        length = len(self.final_word)
        center = (length - 1) / 2
        output = []
        for index in range(length):
            if abs(index - center) > self.radius:
                output.append(" ")
                continue
            if self.solved[index]:
                output.append(self.final_word[index])
            elif self.sched.rng.random() < self.RANDOM_UNDERSCORE_CHANCE:
                self.solved[index] = True
                output.append(self.final_word[index])
            else:
                output.append("_")
        self.element.text = "".join(output)

    def _step(self) -> None:
        self._render()
        self.radius += 0.45
        if all(self.solved):
            self.element.text = self.final_word
            return
        length = len(self.final_word)
        delay = max(min((self.FRAME_DELAY / length) * 12, self.FRAME_DELAY), self.FRAME_DELAY / 2)
        self.sched.set_timeout(delay, self._step)

    def render(self) -> Image.Image:
        """Alpha mask of the centre region (white text), CENTER_W x CENTER_H at y=CENTER_Y."""
        mask = Image.new("L", (CENTER_W, CENTER_H), 0)
        lines = wrap(WORD, self.element.text, CENTER_W - 40)  # .word padding 0 20px
        if not lines:
            return mask
        draw = ImageDraw.Draw(mask)
        top = CANVAS_H / 2 - len(lines) * WORD.line_height / 2 - CENTER_Y
        for line in lines:
            draw_line(draw, WORD, CENTER_W / 2 - _width(WORD, line) / 2, top, line, fill=255)
            top += WORD.line_height
        return mask


def display_text(fields: Dict[str, str]) -> str:
    """typewriter_common.js getDisplayText() for an {artist, title} payload."""
    artist = (fields.get("artist") or "").strip()
    title = (fields.get("title") or "").strip()
    if artist and title:
        return f"{artist} - {title}"
    return title or artist or ""


class OverlayRenderer:
    """Owns both overlays and produces raw frames for FFmpeg, re-rendering only on change.

    Frames whose key is in `cache` (zlib-compressed, filled ahead of time by
    livestream.prerender) are decompressed instead of rendered.
    """

    def __init__(self, fps: float) -> None:
        self.fps = fps
        self.frame = 0
        self.sched = Scheduler()
        self.panel = NowPlayingPanel(self.sched)
        self.title = TypewriterTitle(self.sched)
        self.cache: Dict[tuple, bytes] = {}
        self.cache_hits = 0
        self.live_renders = 0
        self._panel_key = None
        self._panel_bytes = b""
        self._title_key = None
        self._title_bytes = b""

    def on_track(
        self,
        fields: Dict[str, str],
        cover: Optional[Image.Image],
        cover_token: str = "",
        seed: Optional[int] = None,
    ) -> None:
        """A track started: same as the websocket messages received by both pages."""
        self.sched.rng.seed(seed)
        self.panel.refresh_cover(cover, cover_token)
        self.panel.update(fields)
        text = display_text(fields)
        if text:
            self.title.show(text)

    # -- determinism support for pre-rendering -------------------------------
    def settled(self) -> bool:
        return self.sched.idle()

    def snapshot(self) -> dict:
        """State of a settled renderer, enough to replay the next transition elsewhere."""
        return {
            "fields": {key: self.panel.fields[key].text for key in FIELD_ORDER},
            "title": self.title.element.text,
            "cover_token": self.panel.cover_token,
            "cover": _image_payload(self.panel.cover_image),
            "cover_background": self.panel.cover_background,
            "cover_opacity": self.panel.cover_opacity(),
        }

    @classmethod
    def from_snapshot(cls, state: dict, fps: float) -> "OverlayRenderer":
        renderer = cls(fps)
        for key in FIELD_ORDER:
            renderer.panel.fields[key].text = state["fields"][key]
        renderer.title.element.text = state["title"]
        renderer.panel.cover_token = state["cover_token"]
        renderer.panel.cover_image = _image_from_payload(state["cover"])
        renderer.panel.cover_background = state["cover_background"]
        opacity = state["cover_opacity"]
        renderer.panel._opacity = (opacity, opacity, 0.0, 0.0)
        # The live renderer applies an event between frames, then advances one frame.
        renderer.frame = 1
        return renderer

    def panel_key(self) -> tuple:
        return (
            "panel",
            tuple(self.panel.fields[key].text for key in FIELD_ORDER),
            round(self.panel.cover_opacity(), 3),
            self.panel.cover_token,
            self.panel.cover_background,
        )

    def title_key(self) -> tuple:
        return ("title", self.title.element.text)

    def render_key(self, key: tuple) -> bytes:
        image = self.panel.render() if key[0] == "panel" else self.title.render()
        return image.tobytes()

    def advance(self) -> None:
        self.sched.advance_to(self.frame * 1000.0 / self.fps)
        self.frame += 1

    def _frame_bytes(self, key: tuple) -> bytes:
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return zlib.decompress(cached)
        self.live_renders += 1
        return self.render_key(key)

    def next_frames(self) -> Tuple[bytes, bytes]:
        """Advance one video frame; returns (panel RGBA bytes, centre alpha bytes)."""
        self.advance()
        panel_key = self.panel_key()
        if panel_key != self._panel_key:
            self._panel_key = panel_key
            self._panel_bytes = self._frame_bytes(panel_key)
        title_key = self.title_key()
        if title_key != self._title_key:
            self._title_key = title_key
            self._title_bytes = self._frame_bytes(title_key)
        return self._panel_bytes, self._title_bytes


def _image_payload(image: Image.Image) -> tuple:
    return (image.mode, image.size, image.tobytes())


def _image_from_payload(payload: tuple) -> Image.Image:
    mode, size, data = payload
    return Image.frombytes(mode, size, data)

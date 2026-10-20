"""Pre-render the overlay frames of the next track change, spread over the current track.

The overlay animation is deterministic once its start state and random seed
are fixed. While a track plays, a low-priority worker process replays the
upcoming transition (erase/type of the fields, cover fade, title decode) from
a snapshot of the settled overlay, renders every distinct frame once, and
sends them back zlib-compressed. At the switch, the live renderer uses the
same seed, so its frames are cache hits instead of renders. Anything missing
(e.g. a track shorter than its own animation) is simply rendered live.

Pacing: the worker estimates the total work after a quick dry run (no
rendering), then sleeps between frames so the job finishes at about
FINISH_AT of the time left, never using more than MAX_DUTY of a core, and
runs at nice 19.
"""

import multiprocessing
import os
import threading
import time
import zlib
from typing import Callable, Dict, Optional

FINISH_AT = 0.6  # aim to be done when 60% of the remaining time has elapsed
MIN_DUTY = 0.01
MAX_DUTY = 0.5
MAX_TRANSITION_SECONDS = 120.0  # safety bound on the replay length
ESTIMATED_RENDER_SECONDS = 0.015  # first guess per frame; refined while rendering


def _worker_main(connection, fps: float) -> None:
    try:
        os.nice(19)
    except OSError:
        pass
    from .overlay import OverlayRenderer  # imported in the child process

    while True:
        job = connection.recv()
        if job is None:
            return
        job_id, state, event, deadline_seconds = job
        started = time.monotonic()

        # Dry run: collect the distinct frame keys in order, without rendering.
        renderer = OverlayRenderer.from_snapshot(state, fps)
        renderer.on_track(**event)
        keys = []
        seen = set()
        for _frame in range(int(MAX_TRANSITION_SECONDS * fps)):
            renderer.advance()
            for key in (renderer.panel_key(), renderer.title_key()):
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
            if renderer.settled():
                break

        # Real run: replay again (same seed) and render each new key once, paced.
        renderer = OverlayRenderer.from_snapshot(state, fps)
        renderer.on_track(**event)
        pending = set(keys)
        per_frame = ESTIMATED_RENDER_SECONDS
        rendered = 0
        budget_end = started + max(1.0, deadline_seconds * FINISH_AT)
        while pending:
            renderer.advance()
            for key in (renderer.panel_key(), renderer.title_key()):
                if key not in pending:
                    continue
                pending.discard(key)
                render_started = time.monotonic()
                data = zlib.compress(renderer.render_key(key), 1)
                elapsed = time.monotonic() - render_started
                per_frame = 0.8 * per_frame + 0.2 * elapsed
                rendered += 1
                connection.send(("frame", job_id, key, data))
                if connection.poll():
                    break  # a newer job is waiting: abandon this one
                # Sleep so the remaining work ends near budget_end, within duty limits.
                remaining_work = len(pending) * per_frame
                remaining_time = max(0.001, budget_end - time.monotonic())
                duty = min(MAX_DUTY, max(MIN_DUTY, remaining_work / remaining_time))
                time.sleep(elapsed * (1.0 / duty - 1.0))
            else:
                if renderer.settled() and pending:
                    pending.clear()  # replay diverged from the dry run; stop
                continue
            break
        connection.send(("done", job_id, rendered, time.monotonic() - started))


class Prerenderer:
    """Main-process side: owns the worker process and fills the renderer's cache."""

    def __init__(self, fps: float, cache: Dict[tuple, bytes], log: Callable[[str], None]) -> None:
        self.fps = fps
        self.cache = cache
        self.log = log
        self._job_id = 0
        self._lock = threading.Lock()
        self._connection = None
        self._process: Optional[multiprocessing.Process] = None

    def _ensure_worker(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._process = context.Process(target=_worker_main, args=(child, self.fps), name="overlay-prerender", daemon=True)
        self._process.start()
        child.close()
        self._connection = parent
        threading.Thread(target=self._receive, args=(parent,), name="prerender-recv", daemon=True).start()

    def submit(self, state: dict, event: dict, deadline_seconds: float) -> None:
        """Pre-render the transition `event` applied to settled `state`, within ~deadline."""
        with self._lock:
            self._ensure_worker()
            self._job_id += 1
            self.cache.clear()
            self._connection.send((self._job_id, state, event, deadline_seconds))

    def _receive(self, connection) -> None:
        while True:
            try:
                message = connection.recv()
            except (EOFError, OSError):
                return
            kind, job_id = message[0], message[1]
            if job_id != self._job_id:
                continue  # result of an abandoned job
            if kind == "frame":
                self.cache[message[2]] = message[3]
            elif kind == "done":
                size = sum(len(value) for value in self.cache.values())
                self.log(
                    f"Overlay: pre-rendered next transition ({message[2]} frames, "
                    f"{size / 1e6:.1f} MB compressed) in {message[3]:.0f}s"
                )

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.send(None)
            except (BrokenPipeError, OSError):
                pass
        if self._process is not None:
            self._process.join(timeout=2)
            if self._process.is_alive():
                self._process.kill()

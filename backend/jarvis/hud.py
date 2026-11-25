"""Screen HUD: the transparent overlay that shows what Jarvis is doing on screen.

The overlay itself is a click-through, always-on-top Tauri window per monitor (ui/hud.html). It listens to the
same WebSocket as the main UI and draws:
  * a lock-on reticle wherever Jarvis clicks, drags, scrolls or types, and a scan sweep when it takes a screenshot
  * an arrow with a caption when Jarvis points at something to teach you (the `point` tool in tools.py)
  * live system stats (CPU, memory, network, battery) from /api/hud/stats

The HUD windows are excluded from screen capture, so they never show up in Jarvis's own screenshots.
"""
from __future__ import annotations

import time

from . import tools


def enabled(settings: dict) -> bool:
    return bool((settings.get("hud") or {}).get("enabled", True))


class Stats:
    """CPU / memory / network / battery for the HUD, cheap enough to poll every second or two."""

    def __init__(self):
        self._net = None          # (time, sent, recv)

    def read(self) -> dict:
        import psutil
        out: dict = {"time": time.strftime("%H:%M:%S")}
        out["cpu"] = round(psutil.cpu_percent(interval=None), 1)
        try:
            out["cores"] = [round(c) for c in psutil.cpu_percent(interval=None, percpu=True)][:16]
        except Exception:  # noqa: BLE001
            out["cores"] = []
        vm = psutil.virtual_memory()
        out["ram"] = round(vm.percent, 1)
        out["ram_used_gb"] = round((vm.total - vm.available) / 2**30, 1)
        out["ram_total_gb"] = round(vm.total / 2**30, 1)
        try:
            n = psutil.net_io_counters()
            now = time.monotonic()
            if self._net:
                dt = max(1e-3, now - self._net[0])
                out["net_up_kbs"] = round((n.bytes_sent - self._net[1]) / 1024 / dt, 1)
                out["net_down_kbs"] = round((n.bytes_recv - self._net[2]) / 1024 / dt, 1)
            self._net = (now, n.bytes_sent, n.bytes_recv)
        except Exception:  # noqa: BLE001
            pass
        try:
            b = psutil.sensors_battery()
            if b is not None:
                out["battery"] = round(b.percent)
                out["charging"] = bool(b.power_plugged)
        except Exception:  # noqa: BLE001
            pass
        try:
            out["uptime_h"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        except Exception:  # noqa: BLE001
            pass
        out["window"] = tools.active_window_title()[:80]
        return out


STATS = Stats()

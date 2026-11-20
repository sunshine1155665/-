#!/usr/bin/env python
"""Generate scene-specific motion3 files for the pet-shell whale-girl model.

``motion3`` files are just parameter keyframe JSON, so the scene idles and
interactions are authored programmatically here (30fps samples, linear
segments) - no Cubism Editor needed, and none possible anyway: the repo only
ships the published ``moc3``, not the ``.cmo3`` source.

Usage:
    python scripts/pet_motion_gen.py --check            # validate only
    python scripts/pet_motion_gen.py --out pet-shell/assets/model/motions
    python scripts/pet_motion_gen.py --register         # also patch c_0120.model3.json

Curve ids are validated against ``c_0120.cdi3.json`` so a typo cannot slip a
dead parameter into the rig.  Looping idles are checked for seamless wrap
(first key value == last key value per curve).
"""

from __future__ import annotations

import argparse
import json
import math
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(REPO_ROOT, "pet-shell", "assets", "model")
CDI_PATH = os.path.join(MODEL_DIR, "c_0120.cdi3.json")
MODEL3_PATH = os.path.join(MODEL_DIR, "c_0120.model3.json")
FPS = 30

# ---------------------------------------------------------------- easing


def linear(u: float) -> float:
    return u


def smooth(u: float) -> float:
    return u * u * (3 - 2 * u)


def ease_out(u: float) -> float:
    return 1 - (1 - u) * (1 - u)


def ease_in(u: float) -> float:
    return u * u


# ---------------------------------------------------------------- motion

TAU = 2 * math.pi


class Motion:
    """One motion3: parameter curves as time functions or explicit keys."""

    def __init__(self, duration: float, loop: bool = False, fps: int = FPS):
        self.duration = float(duration)
        self.loop = bool(loop)
        self.fps = int(fps)
        self._fns: dict[str, object] = {}
        self._keys: dict[str, list] = {}
        self._wraps: dict[str, float] = {}

    def wrap(self, cid: str, modulus: float) -> "Motion":
        """Declare a value modulus so e.g. 0 -> 360 degrees loops seamlessly."""
        self._wraps[cid] = float(modulus)
        return self

    # -- whole-curve time function (sampled uniformly at fps) ------------
    def fn(self, cid: str, f) -> "Motion":
        self._fns[cid] = f
        return self

    # -- explicit keys [(t, v, ease?)]; ease shapes the segment INTO t ----
    def keys(self, cid: str, pts: list) -> "Motion":
        self._keys[cid] = pts
        return self

    # -- choreography helpers --------------------------------------------
    def hold(self, cid: str, value: float) -> "Motion":
        self.keys(cid, [(0, value), (self.duration, value)])
        return self

    def waves(self, cid: str, center: float, amp: float, period: float, phase: float = 0.0):
        return self.fn(cid, lambda t: center + amp * math.sin(TAU * t / period + phase))

    def sweep(self, cid: str, v0: float, v1: float):
        return self.fn(cid, lambda t: v0 + (v1 - v0) * (t / self.duration))

    def blink(self, cid: str, at: float, open_value: float = 1.0):
        return self.keys(cid, [
            (0, open_value),
            (max(0.0, at - 0.04), open_value, linear),
            (at, 0.05, ease_in),
            (at + 0.1, open_value, ease_out),
            (self.duration, open_value, linear),
        ])

    def pulse(self, cid: str, base: float, peak: float, at: float, attack: float = 0.15, release: float = 0.35):
        return self.keys(cid, [
            (0, base),
            (max(0.0, at - attack), base, linear),
            (at, peak, ease_out),
            (at + release, base, smooth),
            (self.duration, base, linear),
        ])

    # -- validation + output ----------------------------------------------
    def _resolve(self, cid: str) -> list:
        if cid in self._fns:
            f = self._fns[cid]
            n = int(round(self.duration * self.fps))
            return [(i / self.fps, float(f(i / self.fps))) for i in range(n + 1)]
        pts = []
        raw = self._keys[cid]
        for index, item in enumerate(raw):
            t, v = float(item[0]), float(item[1])
            ease = item[2] if len(item) > 2 else None
            if index == 0 or ease is None:
                pts.append((t, v))
                continue
            t0, v0 = pts[-1]
            steps = max(1, int(round((t - t0) * self.fps)))
            for i in range(1, steps + 1):
                u = i / steps
                pts.append((t0 + (t - t0) * u, v0 + (v - v0) * ease(u)))
        if pts[-1][0] < self.duration:
            pts.append((self.duration, pts[-1][1]))  # hold the final pose
        return pts

    def to_json(self) -> dict:
        curves = []
        total_segments = 0
        total_points = 0
        for cid in sorted(set(self._fns) | set(self._keys)):
            pts = self._resolve(cid)
            if len(pts) < 2:
                raise ValueError(f"{cid}: curve needs >= 2 keys")
            if abs(pts[-1][0] - self.duration) > 1e-6:
                raise ValueError(f"{cid}: last key {pts[-1][0]} != duration {self.duration}")
            if self.loop:
                delta = abs(pts[0][1] - pts[-1][1])
                modulus = self._wraps.get(cid)
                if modulus and delta <= modulus:
                    delta = delta % modulus
                if delta > 0.05:
                    raise ValueError(
                        f"{cid}: loop not seamless ({pts[0][1]:.3f} -> {pts[-1][1]:.3f})"
                    )
            for prev, nxt in zip(pts, pts[1:]):
                if nxt[0] <= prev[0]:
                    raise ValueError(f"{cid}: key times not strictly increasing")
            if self.loop and len(pts) >= 4:
                # C1 seam check: value-continuous loops can still flip velocity
                # at the wrap (sine with a non-integer period count does this)
                v_in = pts[1][1] - pts[0][1]
                v_out = pts[-1][1] - pts[-2][1]
                dt_in = max(1e-6, pts[1][0] - pts[0][0])
                dt_out = max(1e-6, pts[-1][0] - pts[-2][0])
                if abs(v_in / dt_in - v_out / dt_out) > 0.5:
                    print(f"WARN {cid}: loop velocity flips at the seam "
                          f"({v_in / dt_in:.2f} -> {v_out / dt_out:.2f}/s)")
            # motion3 Segments layout: [t0, v0, type, t1, v1, ...]  (no keyform
            # prefix - the Cubism parser reads points[0] straight from the two
            # first values, verified against the stock rig's own motion files).
            segments = [pts[0][0], pts[0][1]]
            for t, v in pts[1:]:
                segments += [0, t, v]
            curves.append({"Target": "Parameter", "Id": cid, "Segments": segments})
            total_segments += len(pts) - 1
            total_points += len(pts)
        return {
            "Version": 3,
            "Meta": {
                "Duration": self.duration,
                "Fps": float(self.fps),
                "Loop": self.loop,
                "AreBeziersRestricted": True,
                "CurveCount": len(curves),
                "TotalSegmentCount": total_segments,
                "TotalPointCount": total_points,
                "UserDataCount": 0,
                "TotalUserDataSize": 0,
            },
            "Curves": curves,
        }


# ------------------------------------------------------- shared fragments


def breath(period: float = 3.2, amp: float = 0.45, center: float = 0.5):
    return lambda t: center + amp * math.sin(TAU * t / period)


def tail_b(segments: int, amp: float, period: float, phase_step: float = 0.7):
    """Traveling sine wave down the 5-segment B_tail rig."""

    def make(index: int):
        def f(t):
            return amp * math.sin(TAU * t / period - index * phase_step)
        return f

    return [make(i) for i in range(segments)]


def add_tail(m: Motion, prefix: str, segments: int, amp: float, period: float, phase_step: float = 0.7):
    fns = tail_b(segments, amp, period, phase_step)
    for i, f in enumerate(fns, start=1):
        m.fn(f"{prefix}_{i}", f)
    return m


def blink_pair(m: Motion, at: float, open_value: float = 1.0):
    m.blink("ParamEyeLOpen", at, open_value)
    m.blink("ParamEyeROpen", at, open_value)
    return m


def ear_twitch(m: Motion, times: list, amp: float = 8.0, base: float = 0.0):
    for i, at in enumerate(times):
        for cid in ("Param102", "Param104"):
            m.pulse(cid, base, base + amp if i % 2 == 0 else base - amp, at,
                    attack=0.06, release=0.12)
    return m


# ------------------------------------------------------------ motions


def d_wave() -> Motion:
    # no arm rig: wave = head+body swing with a fast tail whoosh
    m = Motion(2.0)
    m.keys("ParamAngleZ", [(0, 0), (0.35, 18, smooth), (0.8, -15, smooth), (1.2, 15, smooth), (1.6, 0, smooth)])
    m.keys("ParamBodyAngleZ", [(0, 0), (0.35, 8, smooth), (0.8, -6, smooth), (1.2, 6, smooth), (1.6, 0, smooth)])
    m.keys("ParamAngleX", [(0, 0), (0.5, -10, smooth), (1.4, 8, smooth), (2.0, 0, smooth)])
    m.hold("ParamMouthForm", 1.0)
    add_tail_ids(m, 34.0, 0.6)
    ear_twitch(m, [0.4, 1.0, 1.6], amp=8.0)
    return m


def poke() -> Motion:
    # jolt reaction: body flinch, eyes pop, tiny "wue" mouth
    m = Motion(1.2)
    m.keys("ParamBodyAngleX", [(0, 0), (0.15, -10, ease_out), (0.45, 4, smooth), (1.2, 0, smooth)])
    m.keys("ParamBodyAngleZ", [(0, 0), (0.2, 7, smooth), (0.55, -4, smooth), (1.2, 0, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.2, 11, smooth), (0.55, -6, smooth), (1.2, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.18, 1, linear), (0.32, 0.08, ease_in), (0.55, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.18, 1, linear), (0.32, 0.08, ease_in), (0.55, 1, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.22, 0.55, ease_out), (0.5, 0, smooth)])
    m.keys("ParamCheek75", [(0, 0), (0.15, 1, ease_out), (0.72, 1, linear), (0.78, 0, smooth)])
    add_tail_ids(m, 20.0, 0.8)
    return m


def shy() -> Motion:
    # blush ramps up, look away and down, fidgety tail
    m = Motion(2.0)
    m.keys("Paramhh2", [(0, 0), (0.4, 1, smooth), (1.6, 1, linear), (2.0, 0.5, smooth)])
    m.keys("ParamAngleY", [(0, 0), (0.5, -14, smooth), (1.5, -14, linear), (2.0, 0, smooth)])
    m.keys("ParamAngleX", [(0, 0), (0.5, -12, smooth), (1.5, -12, linear), (2.0, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.6, 0.7, smooth), (1.5, 0.7, linear), (2.0, -0.5, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.55, 0.45, smooth), (1.5, 0.45, linear), (2.0, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.55, 0.45, smooth), (1.5, 0.45, linear), (2.0, 1, smooth)])
    add_tail_ids(m, 18.0, 1.1)
    return m


def surprised() -> Motion:
    # startle: brows up, mouth open, head and body recoil
    m = Motion(1.1)
    m.keys("ParamBrowLY", [(0, 0), (0.12, 0.9, ease_out), (0.7, 0.9, linear), (1.1, 0, smooth)])
    m.keys("ParamBrowRY", [(0, 0), (0.12, 0.9, ease_out), (0.7, 0.9, linear), (1.1, 0, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.12, 0.9, ease_out), (0.6, 0.55, smooth), (1.1, 0, smooth)])
    m.keys("ParamCheek75", [(0, 0), (0.12, 1, ease_out), (0.82, 1, linear), (0.88, 0, smooth)])
    m.keys("ParamAngleY", [(0, 0), (0.12, 16, ease_out), (0.7, 16, linear), (1.1, 0, smooth)])
    m.keys("ParamBodyAngleX", [(0, 0), (0.15, 8, ease_out), (0.7, 8, linear), (1.1, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (1.1, 1, linear)])
    m.keys("ParamEyeROpen", [(0, 1), (1.1, 1, linear)])
    add_tail_ids(m, 26.0, 0.7)
    return m


def headpat() -> Motion:
    # press into the palm, melt, happy squint + blush
    m = Motion(2.2)
    m.keys("ParamAngleY", [(0, 0), (0.3, 10, smooth), (0.7, 6, smooth), (1.4, 5, linear), (2.2, 0, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (0.4, 4, smooth), (1.5, 3, linear), (2.2, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.45, 0.12, smooth), (1.8, 0.12, linear), (2.1, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.45, 0.12, smooth), (1.8, 0.12, linear), (2.1, 1, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.6, 1.0, smooth), (1.8, 1.0, linear), (2.2, -0.5, smooth)])
    m.keys("Paramhh2", [(0, 0), (0.5, 0.8, smooth), (1.8, 0.8, linear), (2.2, 0, smooth)])
    add_tail_ids(m, 24.0, 1.4)
    return m


def blink_at(m: Motion, times: list, open_value: float = 1.0):
    """Full two-stroke blinks at several uneven phases in one curve."""
    pts = [(0, open_value)]
    for t in times:
        pts += [(t - 0.06, open_value, linear), (t, 0.05, ease_in), (t + 0.1, open_value, ease_out)]
    pts.append((m.duration, open_value, linear))
    m.keys("ParamEyeLOpen", pts)
    m.keys("ParamEyeROpen", pts)
    return m


def add_tail_ids(m: Motion, amp: float, period: float, phase_step: float = 0.7):
    for i in range(1, 6):
        m.fn(f"Param_Angle_Rotation_{i}_B_tail3",
             (lambda i: lambda t: amp * math.sin(TAU * t / period - i * phase_step))(i))
    return m


def d_look() -> Motion:
    m = Motion(2.4)
    m.keys("ParamAngleX", [(0, 0), (0.8, -22, smooth), (1.8, 18, smooth), (2.4, 0, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.8, -14, smooth), (1.8, 12, smooth), (2.4, 0, smooth)])
    m.keys("ParamEyeBallX", [(0, 0), (0.8, -0.9, smooth), (1.8, 0.85, smooth), (2.4, 0, smooth)])
    m.keys("ParamBodyAngleX", [(0, 0), (0.8, -5, smooth), (1.8, 5, smooth), (2.4, 0, smooth)])
    blink_pair(m, 1.5)
    return m


def d_stretch() -> Motion:
    # single closed-to-open eye transition (no blink collisions mid-stretch)
    m = Motion(2.8)
    m.keys("ParamBreath", [(0, 0), (1.0, 1.0, smooth), (1.9, 1.0, linear), (2.8, 0, smooth)])
    m.keys("ParamAngleY", [(0, 0), (1.0, 22, smooth), (2.0, 14, linear), (2.8, 0, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (1.0, 6, smooth), (2.0, 4, linear), (2.8, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.35, 0.06, smooth), (2.0, 0.06, linear), (2.3, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.35, 0.06, smooth), (2.0, 0.06, linear), (2.3, 1, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (1.1, 0.75, smooth), (1.9, 0.5, linear), (2.5, 0, smooth)])
    add_tail_ids(m, 24.0, 1.3)
    return m


def d_ear() -> Motion:
    m = Motion(1.4)
    ear_twitch(m, [0.2, 0.45, 0.7, 0.95], amp=18.0)
    m.keys("ParamAngleZ", [(0, 0), (0.4, 8, smooth), (1.0, -6, smooth), (1.4, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.3, 0.8, smooth), (1.4, -0.5, smooth)])
    return m


def idle_work() -> Motion:
    m = Motion(6.0, loop=True)
    m.waves("ParamBodyAngleX", 0, 2.0, 6.0)
    m.fn("ParamBreath", breath(period=3.0, amp=0.4, center=0.5))
    m.waves("ParamEyeBallX", 0, 0.7, 6.0)
    m.fn("ParamEyeBallY", lambda t: 0.75 * max(0.0, math.sin(TAU * t / 6.0)) ** 3)  # think-pause gaze lift per 6s
    m.fn("ParamAngleY", lambda t: -6.0 + 1.6 * math.sin(TAU * t / 3.0) + 4.0 * max(0.0, math.sin(TAU * t / 6.0)) ** 3)  # sway + look-up on the pause
    m.waves("ParamBodyAngleY", 0, 1.8, 1.2)  # typing rhythm (visible channel)
    add_tail_ids(m, 10.0, 3.0)
    # periodic typing blinks (4 dips per 6s loop, wrap-safe via same phase)
    blink_at(m, [1.7, 4.6])
    m.hold("ParamCheek70", 1.0)  # keep round glasses through the idle
    return m


def w_type() -> Motion:
    m = Motion(2.4)
    m.waves("ParamBodyAngleY", 0, 2.5, 0.35)  # typing rhythm
    m.waves("ParamEyeBallX", 0, 0.7, 0.5)  # keyboard/screen gaze darting
    m.keys("ParamAngleY", [(0, 0), (0.3, -8, smooth), (2.0, -8, linear), (2.4, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.5, 0.2, smooth), (2.4, -0.5, smooth)])
    add_tail_ids(m, 16.0, 0.8)
    m.keys("ParamEyeROpen", [(0, 1), (1.9, 1, linear), (2.1, 0.15, ease_in), (2.4, 1, ease_out)])
    return m


def w_think() -> Motion:
    m = Motion(2.8)
    m.keys("ParamAngleZ", [(0, 0), (0.6, 16, smooth), (2.4, 16, linear), (2.8, 0, smooth)])
    m.keys("ParamEyeBallX", [(0, 0), (0.6, -0.75, smooth), (2.2, -0.75, linear), (2.8, 0, smooth)])
    m.keys("ParamEyeBallY", [(0, 0), (0.6, 0.8, smooth), (2.2, 0.8, linear), (2.8, 0, smooth)])
    m.keys("ParamBrowLAngle", [(0, 0), (0.6, 0.7, smooth), (2.2, 0.7, linear), (2.8, 0, smooth)])
    m.keys("ParamBrowLY", [(0, 0), (0.6, -0.8, smooth), (2.4, -0.8, linear), (2.8, 0, smooth)])
    m.keys("ParamBrowRY", [(0, 0), (0.6, -0.8, smooth), (2.4, -0.8, linear), (2.8, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.7, -1.1, smooth), (1.6, -0.7, smooth), (2.8, -0.5, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (1.4, 0.22, smooth), (1.8, 0, smooth)])
    add_tail_ids(m, 0.0, 2.0)
    return m


def w_sip() -> Motion:
    # no cup prop in rig: tea break = lean back, savour, happy exhale
    m = Motion(2.8)
    m.keys("ParamEyeBallY", [(0, 0), (0.25, -0.6, smooth), (0.6, -0.3, smooth), (1.8, -0.3, linear), (2.2, 0, smooth)])
    m.keys("ParamEyeBallX", [(0, 0), (0.25, 0.55, smooth), (1.8, 0.55, linear), (2.2, 0, smooth)])
    m.keys("ParamAngleX", [(0, 0), (0.4, 8, smooth), (1.8, 8, linear), (2.2, 0, smooth)])
    m.keys("ParamAngleY", [(0, 0), (0.6, -18, smooth), (1.8, -18, linear), (2.4, 0, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (0.6, 6, smooth), (1.8, 6, linear), (2.4, 0, smooth)])
    m.keys("ParamMouthOpenY", [
        (0, 0), (0.8, 0.35, ease_out), (1.2, 0.08, smooth),
        (1.4, 0.3, ease_out), (1.8, 0.05, smooth), (2.8, 0, linear),
    ])
    m.keys("ParamMouthForm", [(0, -0.5), (2.0, 1.0, smooth), (2.8, -0.5, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (1.9, 0.3, smooth), (2.4, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (1.9, 0.3, smooth), (2.4, 1, smooth)])
    add_tail_ids(m, 14.0, 1.0)
    return m


def w_stretch() -> Motion:
    # back arch + closed eyes + big yawn, then melt back down
    m = Motion(3.4)
    m.keys("ParamBreath", [(0, 0), (1.2, 1.0, smooth), (2.1, 1.0, linear), (3.4, 0, smooth)])
    m.keys("ParamAngleY", [(0, 0), (1.2, 26, smooth), (2.1, 16, linear), (3.4, 0, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (1.2, 10, smooth), (2.1, 7, linear), (3.4, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.6, 0.08, smooth), (2.3, 0.08, linear), (2.7, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.6, 0.08, smooth), (2.3, 0.08, linear), (2.7, 1, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (1.3, 0.85, smooth), (2.1, 0.55, linear), (2.8, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (1.4, -1.1, smooth), (2.7, -0.5, smooth)])
    add_tail_ids(m, 22.0, 1.6)
    return m


def w_cheer() -> Motion:
    # no arm raise: cheer = star-eyes flash + happy hops of the head + tail whir
    m = Motion(2.0)
    m.keys("ParamCheek16", [(0, 0), (0.25, 1, ease_out), (1.3, 1, linear), (1.7, 0, smooth)])
    m.keys("ParamAngleY", [(0, 0), (0.3, 16, ease_out), (0.7, 0, smooth), (1.0, 14, ease_out), (1.4, 0, smooth), (2.0, 0, linear)])
    m.keys("ParamAngleZ", [(0, 0), (0.45, 10, smooth), (0.95, -9, smooth), (1.4, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.3, 1.2, smooth), (1.6, 1.2, linear), (2.0, -0.5, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.35, 0.5, ease_out), (0.7, 0.1, smooth), (1.05, 0.45, ease_out), (1.5, 0, smooth)])
    add_tail_ids(m, 32.0, 0.5)
    return m


def idle_meal() -> Motion:
    m = Motion(6.0, loop=True)
    def _meal_mouth(t):
        ph = t % 6.0
        if ph < 4.2:
            return 0.35 + 0.3 * math.sin(TAU * ph / 0.75)
        if ph < 5.2:
            return 0.05  # pause and swallow
        return 0.35 + 0.3 * math.sin(TAU * ph / 0.75)
    m.fn("ParamMouthOpenY", _meal_mouth)
    m.waves("ParamMouthForm", 0.1, 0.2, 0.75)  # 8 cycles per 6s loop (C1-seamless)
    m.waves("ParamBodyAngleZ", 0, 2.0, 2.0)  # sway with the pot, not a conveyor
    m.waves("danbaoY", 0.8, 2.2, 2.0)  # spoon idle bob
    m.waves("danbaoX", 0.8, 1.0, 2.0)  # pot sways with the bob
    m.waves("danbaoz", 0, 0.5, 2.0)    # pot tilts as it bobs
    def _meal_eyes(t):
        ph = t % 6.0
        base = 0.8 + 0.2 * math.sin(TAU * ph / 3.0)
        down = max(0.0, min(1.0, (ph - 1.85) / 0.15))
        up = max(0.0, min(1.0, (ph - 2.7) / 0.15))
        squint = down * (1 - up)
        return base * (1 - squint) + 0.35 * squint
    m.fn("ParamEyeLOpen", _meal_eyes)  # happy squint while chewing, soft edges
    m.fn("ParamEyeROpen", _meal_eyes)
    m.fn("ParamBreath", breath(period=3.0, amp=0.4, center=0.5))
    add_tail_ids(m, 12.0, 3.0)
    m.hold("danbaofan", 1.0)
    m.hold("point", 0.0)
    return m


def m_bite() -> Motion:
    # spoon from the plate to the mouth, bite, chew (can prop removed from outfit)
    m = Motion(2.4)
    m.keys("danbaoY", [(0, 0), (0.5, 6.5, ease_out), (0.8, 6.5, linear), (1.2, 0, ease_in), (2.4, 0, linear)])
    m.keys("danbaoX", [(0, 0), (0.5, 1.4, ease_out), (1.2, 0, ease_in), (2.4, 0, linear)])
    m.keys("ParamAngleY", [(0, 0), (0.55, -10, smooth), (1.0, -10, linear), (1.5, 0, smooth)])
    m.keys("ParamMouthOpenY", [
        (0, 0), (0.5, 0.65, ease_out), (0.8, 0.12, smooth),
        (1.0, 0.45, ease_out), (1.3, 0.12, smooth),
        (1.5, 0.4, ease_out), (1.85, 0.1, smooth), (2.4, 0, linear),
    ])
    m.keys("ParamMouthForm", [(0, -0.5), (2.0, 0.9, smooth), (2.4, -0.5, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (1.9, 0.3, smooth), (2.4, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (1.9, 0.3, smooth), (2.4, 1, smooth)])
    return m


def m_parfait() -> Motion:
    # parfait treat: spoon dips toward the cup, wink of delight
    m = Motion(2.6)
    m.keys("ParamAngleY", [(0, 0), (0.5, -16, smooth), (2.0, -14, linear), (2.6, 0, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (0.5, 4, smooth), (2.0, 4, linear), (2.6, 0, smooth)])
    m.keys("danbaoY", [(0, 0), (0.6, 5.5, ease_out), (1.1, 5.5, linear), (1.5, 0, ease_in), (2.6, 0, linear)])
    m.keys("ParamMouthOpenY", [
        (0, 0), (0.75, 0.5, ease_out), (1.05, 0.08, smooth),
        (1.3, 0.4, ease_out), (1.7, 0.08, smooth), (2.6, 0, linear),
    ])
    m.keys("ParamMouthForm", [(0, -0.5), (2.0, 1.0, smooth), (2.6, -0.5, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.6, 0.2, smooth), (2.0, 0.2, linear), (2.2, -1, smooth), (2.6, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.6, 0.2, smooth), (2.2, 0.2, linear), (2.6, 1, smooth)])
    add_tail_ids(m, 16.0, 1.2)
    return m


def m_gulp() -> Motion:
    # tilt head back, big swallows, satisfied finish
    m = Motion(2.4)
    m.keys("ParamAngleY", [(0, 0), (0.5, 24, smooth), (1.8, 24, linear), (2.4, 0, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (0.5, -5, smooth), (1.8, -5, linear), (2.4, 0, smooth)])
    m.keys("ParamMouthOpenY", [
        (0, 0), (0.6, 0.6, ease_out), (0.95, 0.15, smooth),
        (1.15, 0.5, ease_out), (1.5, 0.1, smooth),
        (1.7, 0.45, ease_out), (2.0, 0.1, smooth), (2.4, 0, linear),
    ])
    m.keys("ParamMouthForm", [(0, -0.5), (2.15, 0.8, smooth), (2.4, -0.5, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.5, 0.35, smooth), (1.9, 0.35, linear), (2.2, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.5, 0.35, smooth), (1.9, 0.35, linear), (2.2, 1, smooth)])
    return m


def m_protect() -> Motion:
    # hunch over the plate + angry glare (生气 brow flash)
    m = Motion(2.2)
    m.keys("ParamAngleX", [(0, 0), (0.4, -22, ease_out), (1.8, -22, linear), (2.2, 0, smooth)])
    m.keys("ParamBodyAngleX", [(0, 0), (0.4, -11, ease_out), (1.8, -11, linear), (2.2, 0, smooth)])
    m.keys("ParamCheek80", [(0, 0), (0.35, 1, ease_out), (1.7, 1, linear), (2.2, 0, smooth)])
    m.keys("ParamBrowLForm", [(0, 0), (0.4, -1.0, ease_out), (1.7, -1.0, linear), (2.2, 0, smooth)])
    m.keys("ParamBrowRForm", [(0, 0), (0.4, -1.0, ease_out), (1.7, -1.0, linear), (2.2, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.4, -1.4, ease_out), (1.7, -1.4, linear), (2.2, -0.5, smooth)])
    m.keys("ParamEyeBallX", [(0, 0), (0.5, 0.6, smooth), (1.6, 0.6, linear), (2.2, 0, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (1.0, -7, smooth), (1.4, 5, smooth), (1.8, 0, smooth)])
    add_tail_ids(m, 18.0, 0.5)
    return m


def m_yummy() -> Motion:
    m = Motion(1.8)
    m.hold("love", 1.0)
    m.keys("Param9", [(0, 0), (0.3, 1.0, ease_out), (1.7, 1.0, linear), (1.8, 0, smooth)])
    m.sweep("aixing", 0, 14)
    m.keys("ParamEyeROpen", [(0, 1), (0.9, 1, linear), (1.1, -1, ease_in), (1.8, 1, ease_out)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.4, 1.2, smooth), (1.5, 1.2, linear), (1.8, -0.5, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.5, 8, smooth), (1.2, -6, smooth), (1.8, 0, smooth)])
    add_tail_ids(m, 26.0, 0.8)
    return m


def idle_clingy() -> Motion:
    m = Motion(6.0, loop=True)
    # paw kneading around the rig's resting pose
    for cid, base, phase in (("maoshou2", 0.30, 0.0), ("maoshou3", -0.32, 1.1),
                             ("maoshou4", -0.73, 2.2), ("maoshou5", -0.15, 3.1),
                             ("maoshou6", 0.10, 4.2), ("maoshou7", -0.73, 5.0)):
        m.waves(cid, base, 0.32, 1.2, phase)
    add_tail_ids(m, 12.0, 2.0)
    m.waves("ParamBodyAngleZ", 0, 4.5, 3.0)
    m.waves("ParamBreath4", 0.7, 0.3, 1.0)  # heartbeat ring, 1Hz
    ear_twitch(m, [2.0, 4.5], amp=6.0)
    m.fn("ParamEyeLOpen", lambda t: 1.0 if (t % 2.0) > 0.16 else 0.2 + 5 * (t % 2.0))
    m.fn("ParamEyeROpen", lambda t: 1.0 if (t % 2.0) > 0.16 else 0.2 + 5 * (t % 2.0))
    m.hold("ParamCheek83", 1.0)  # cat ears stay on
    m.hold("maoshou", 1.0)
    m.hold("ParamCheek81", 1.0)
    return m


def c_bap() -> Motion:
    # paw deco (c3-c6) swats + chest paw re-pose (maoshou*) + head follows
    m = Motion(1.6)
    m.keys("c3", [(0, 0.32), (0.25, 1.0, ease_out), (0.5, 0.32, smooth), (0.75, 1.0, ease_out), (1.1, 0.32, smooth)])
    m.keys("c5", [(0, 0.41), (0.3, 1.0, ease_out), (0.55, 0.41, smooth), (0.8, 1.0, ease_out), (1.15, 0.41, smooth)])
    m.keys("c4", [(0, 0), (0.25, 1.0, ease_out), (0.5, -0.6, smooth), (0.75, 1.0, ease_out), (1.1, 0, smooth)])
    m.keys("c6", [(0, 0), (0.3, 1.0, ease_out), (0.55, -0.6, smooth), (0.8, 1.0, ease_out), (1.15, 0, smooth)])
    m.keys("maoshou2", [(0, 0), (0.3, 1.0, ease_out), (0.6, 0, smooth)])
    m.keys("maoshou5", [(0, 0), (0.8, -1.0, ease_out), (1.2, 0, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (0.3, 8, ease_out), (1.6, 0, smooth)])
    m.keys("ParamAngleY", [(0, 0), (0.3, -8, ease_out), (1.6, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.4, 1.2, smooth), (1.6, -0.5, smooth)])
    add_tail_ids(m, 20.0, 0.9)
    return m


def c_rub() -> Motion:
    # big cheek rub: head zigzags against you, purr face, ears flutter
    m = Motion(2.2)
    m.keys("ParamAngleX", [(0, 0), (0.4, 20, smooth), (0.8, -20, smooth), (1.2, 18, smooth), (1.6, -14, smooth), (2.2, 0, smooth)])
    m.keys("ParamBodyAngleX", [(0, 0), (0.4, 9, smooth), (0.8, -9, smooth), (1.2, 8, smooth), (1.6, -6, smooth), (2.2, 0, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.5, 10, smooth), (1.4, 10, linear), (2.2, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.4, 1.2, smooth), (1.7, 1.2, linear), (2.2, -0.5, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.4, 0.15, smooth), (1.7, 0.15, linear), (2.2, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.4, 0.15, smooth), (1.7, 0.15, linear), (2.2, 1, smooth)])
    ear_twitch(m, [0.7, 1.2, 1.7], amp=7.0)
    add_tail_ids(m, 16.0, 1.2)
    return m


def c_purr() -> Motion:
    m = Motion(2.6)
    m.hold("love", 1.0)
    m.keys("ParamEyeLOpen", [(0, 1), (0.3, 0.12, smooth), (2.3, 0.12, linear), (2.6, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.3, 0.12, smooth), (2.3, 0.12, linear), (2.6, 1, smooth)])
    m.hold("ParamCheek23", 1.0)  # wavy purr mouth
    m.waves("Param102", 0, 3.0, 0.35)
    m.waves("Param104", 0, 3.0, 0.35, 0.9)
    add_tail_ids(m, 10.0, 2.0)
    m.keys("Param9", [(0, 0), (1.8, 1.0, ease_out), (2.5, 1.0, linear), (2.6, 0, smooth)])
    m.sweep("aixing", 0, 14)
    return m


def c_dangle() -> Motion:
    m = Motion(1.8)
    m.waves("c3", 0.6, 0.7, 0.22)
    m.waves("c5", 0.7, 0.7, 0.22, 1.6)
    m.waves("c4", 0, 0.6, 0.22)
    m.waves("c6", 0, 0.6, 0.22, 1.6)
    add_tail_ids(m, 38.0, 0.55)
    m.waves("ParamBodyAngleZ", 0, 5.0, 0.5)
    m.hold("ParamEyeLOpen", 1.0)
    m.hold("ParamEyeROpen", 1.0)
    m.keys("ParamMouthOpenY", [(0, 0), (0.25, 0.6, ease_out), (1.4, 0.5, linear), (1.8, 0, smooth)])
    return m


def c_nuzzle() -> Motion:
    m = Motion(1.8)
    m.keys("ParamAngleY", [(0, 0), (0.35, 15, ease_out), (1.3, 15, linear), (1.8, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.4, 0.1, smooth), (1.4, 0.1, linear), (1.8, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.4, 0.1, smooth), (1.4, 0.1, linear), (1.8, 1, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.5, 1.0, smooth), (1.8, -0.5, smooth)])
    m.hold("love", 1.0)
    m.keys("Param9", [(0, 0), (0.7, 1.0, ease_out), (1.6, 1.0, linear), (1.8, 0, smooth)])
    m.sweep("aixing", 0, 14)
    ear_twitch(m, [0.9], amp=6.0)
    return m


def idle_happy() -> Motion:
    m = Motion(5.0, loop=True)
    m.waves("ParamBodyAngleZ", 0, 2.5, 2.5)  # buoyancy sway (visible channel)
    add_tail_ids(m, 28.0, 1.0)
    m.sweep("ParamCheek28", 0, 360).wrap("ParamCheek28", 360)  # flower spin
    m.waves("ParamBreath3", -1.39, 1.0, 2.5)  # aura ring wobble
    m.waves("Param73", 0.55, 0.45, 1.25)  # star twinkle
    m.hold("ParamMouthForm", 1.0)  # resting smile (clamped max)
    m.waves("ParamMouthOpenY", 0.25, 0.15, 1.25)  # open smile breathing (readable when eyes open)
    m.hold("ParamBrowLY", -1.0)
    m.hold("ParamBrowRY", -1.0)
    blink_at(m, [3.1])
    m.hold("ParamCheek26", 360)
    m.hold("ParamCheek81", 1.0)
    return m


def h_jump() -> Motion:
    # no body-lift param: joy hop = rapid happy nods + tail thrash + hearts
    m = Motion(2.0)
    m.hold("love", 1.0)
    m.keys("ParamAngleY", [(0, 0), (0.3, 20, ease_out), (0.65, 0, smooth), (0.95, 16, ease_out), (1.3, 0, smooth), (2.0, 0, linear)])
    m.keys("ParamBodyAngleZ", [(0, 0), (0.35, 6, smooth), (0.7, 0, smooth), (1.0, -6, smooth), (1.4, 0, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.3, 0.6, ease_out), (0.6, 0.1, smooth), (0.95, 0.5, ease_out), (1.4, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.3, 1.2, smooth), (1.5, 1.2, linear), (2.0, -0.5, smooth)])
    m.keys("Param9", [(0, 0), (0.4, 1.0, ease_out), (1.8, 1.0, linear), (2.0, 0, smooth)])
    m.sweep("aixing", 0, 14)
    add_tail_ids(m, 34.0, 0.55)
    return m


def h_spin() -> Motion:
    # spin = big head+body rotation sweep (visible silhouette change)
    m = Motion(2.0)
    m.keys("ParamAngleZ", [(0, 0), (0.55, 26, smooth), (1.25, -26, smooth), (1.75, 0, smooth)])
    m.keys("ParamBodyAngleZ", [(0, 0), (0.55, 10, smooth), (1.25, -10, smooth), (1.75, 0, smooth)])
    m.keys("ParamAngleX", [(0, 0), (0.55, 18, smooth), (1.25, -18, smooth), (1.75, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.35, 0.3, smooth), (1.4, 0.3, linear), (1.75, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.35, 0.3, smooth), (1.4, 0.3, linear), (1.75, 1, smooth)])
    m.hold("ParamMouthForm", 1.0)
    add_tail_ids(m, 30.0, 0.8)
    return m


def h_glee() -> Motion:
    # delight: hearts burst + brow lift + bright grin + wiggle
    m = Motion(1.8)
    m.hold("love", 1.0)
    m.keys("Param9", [(0, 0), (0.25, 1.0, ease_out), (1.5, 1.0, linear), (1.8, 0, smooth)])
    m.sweep("aixing", 0, 14)
    m.keys("ParamBrowLY", [(0, 0), (0.3, -0.6, smooth), (1.4, -0.6, linear), (1.8, 0, smooth)])
    m.keys("ParamBrowRY", [(0, 0), (0.3, -0.6, smooth), (1.4, -0.6, linear), (1.8, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.3, 1.3, smooth), (1.4, 1.3, linear), (1.8, -0.5, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.35, 0.45, ease_out), (0.7, 0.05, smooth), (1.05, 0.4, ease_out), (1.5, 0, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.5, 13, smooth), (1.1, -10, smooth), (1.8, 0, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (1.0, 1, linear), (1.2, -1, ease_in), (1.8, 1, ease_out)])
    add_tail_ids(m, 30.0, 0.6)
    return m


def h_boing() -> Motion:
    # boing = springy head pops with squash-pose alternation + tail coil
    m = Motion(1.8)
    m.keys("ParamAngleY", [(0, 0), (0.25, 20, ease_out), (0.5, 2, smooth), (0.8, 16, ease_out), (1.1, 0, smooth), (1.4, 12, ease_out), (1.8, 0, smooth)])
    m.keys("ParamBodyAngleZ", [(0, 0), (0.3, 10, smooth), (0.6, -7, smooth), (1.0, 7, smooth), (1.4, -5, smooth), (1.8, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.3, 1.2, smooth), (1.5, 1.2, linear), (1.8, -0.5, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.3, 0.35, smooth), (0.7, 1, smooth), (1.0, 0.35, smooth), (1.4, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.3, 0.35, smooth), (0.7, 1, smooth), (1.0, 0.35, smooth), (1.4, 1, smooth)])
    add_tail_ids(m, 36.0, 0.45)
    return m


def h_loveburst() -> Motion:
    m = Motion(2.2)
    m.hold("love", 1.0)
    m.keys("Param9", [(0, 0), (0.2, 1.0, ease_out), (2.0, 1.0, linear), (2.2, 0, smooth)])
    m.keys("aixing", [(0, 0), (1.0, 14, linear), (1.1, 0, linear), (2.1, 14, linear), (2.2, 0, smooth)])
    m.keys("ParamBodyAngleZ", [(0, 0), (0.4, 5, ease_out), (0.8, -4, ease_in), (1.2, 5, ease_out), (1.6, 0, ease_in), (2.2, 0, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.5, 0.45, ease_out), (1.0, 0, smooth), (1.5, 0.4, ease_out), (1.9, 0, smooth)])
    add_tail_ids(m, 24.0, 0.9)
    return m


def idle_night() -> Motion:
    m = Motion(8.0, loop=True)
    m.hold("ParamEyeLOpen", 0.06)
    m.hold("ParamEyeROpen", 0.06)
    # Cheek22 (呆2) removed: its glassy-eye layer ghosts over the closed lids  # drool
    m.fn("ParamBreath", breath(period=4.0, amp=0.5, center=0.5))
    m.waves("ParamAngleY", -4.0, 3.0, 4.0)  # nodding dips
    m.waves("ParamBodyAngleZ", 0, 2.0, 8.0)
    m.keys("paopao", [(0, 0.15), (2.0, 1.0, smooth), (2.5, 1.0, linear), (2.8, 0.15, smooth),
                      (4.0, 0.15, linear), (6.0, 1.0, smooth), (6.5, 1.0, linear), (6.8, 0.15, smooth), (8.0, 0.15, linear)])
    m.keys("paopao2", [(0, 0), (2.5, 0, linear), (2.65, 1.0, ease_out), (2.9, 0, smooth),
                       (6.5, 0, linear), (6.65, 1.0, ease_out), (6.9, 0, smooth), (8.0, 0, linear)])
    m.hold("paopao3", 1.0)
    m.hold("ParamMouthOpenY", 0.12)
    add_tail_ids(m, 6.0, 4.0)
    return m


def n_wakeup() -> Motion:
    # sleepy → stir → eyes open → blink → awake
    m = Motion(3.0)
    m.keys("ParamEyeLOpen", [(0, 0.05), (1.3, 0.05, linear), (1.7, 1, smooth), (2.0, 0.25, smooth), (2.3, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 0.05), (1.3, 0.05, linear), (1.7, 1, smooth), (2.0, 0.25, smooth), (2.3, 1, smooth)])
    m.keys("ParamAngleY", [(0, 0), (1.0, -9, smooth), (1.8, 6, smooth), (2.6, 0, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.8, 0.6, smooth), (1.3, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (2.4, 0.7, smooth), (3.0, -0.5, smooth)])
    m.keys("ParamBodyAngleY", [(0, 0), (1.1, 4, smooth), (2.4, 0, smooth)])
    add_tail_ids(m, 16.0, 2.6)
    return m


def n_drowse() -> Motion:
    m = Motion(2.4)
    m.keys("ParamEyeLOpen", [(0, 0.06), (0.4, 0.35, smooth), (0.7, 0.1, smooth), (1.0, 0.35, smooth), (1.3, 0.06, smooth), (2.4, 0.06, linear)])
    m.keys("ParamEyeROpen", [(0, 0.06), (0.4, 0.35, smooth), (0.7, 0.1, smooth), (1.0, 0.35, smooth), (1.3, 0.06, smooth), (2.4, 0.06, linear)])
    m.keys("ParamMouthOpenY", [
        (0, 0.12), (0.6, 0.25, ease_out), (0.9, 0.1, smooth),
        (1.3, 0.25, ease_out), (1.6, 0.1, smooth), (2.4, 0.12, linear),
    ])
    m.waves("ParamAngleZ", 0, 3.0, 0.8)
    m.keys("ParamAngleY", [(0, -4), (0.8, -2, smooth), (1.6, -7, smooth), (2.4, -4, smooth)])
    return m


def n_float() -> Motion:
    m = Motion(2.4)
    m.waves("ParamBodyAngleX", 0, 6.0, 1.6)
    m.waves("ParamBodyAngleZ", 0, 3.0, 2.0)
    m.waves("ParamAngleZ", 0, 6.0, 2.4)
    m.hold("ParamEyeLOpen", 0.06)
    m.hold("ParamEyeROpen", 0.06)
    # Cheek22 (呆2) removed: its glassy-eye layer ghosts over the closed lids
    m.hold("paopao3", 1.0)
    m.fn("ParamBreath", breath(period=3.0, amp=0.5, center=0.5))
    return m


def n_dream() -> Motion:
    m = Motion(2.6)
    m.keys("ParamMouthOpenY", [
        (0, 0.12), (0.5, 0.3, ease_out), (0.8, 0.1, smooth),
        (1.2, 0.28, ease_out), (1.5, 0.1, smooth),
        (1.9, 0.25, ease_out), (2.2, 0.1, smooth), (2.6, 0.12, linear),
    ])
    m.keys("ParamMouthForm", [(0, 0.2), (0.6, 1.0, smooth), (2.6, 0.8, linear)])  # dreaming smile
    m.keys("paopao", [(0, 0.3), (1.8, 1.0, smooth), (2.2, 1.0, linear), (2.4, 0.3, smooth)])
    m.keys("paopao2", [(0, 0), (2.3, 0, linear), (2.45, 1.0, ease_out), (2.6, 0, smooth)])
    # Cheek22 ghost layer off
    add_tail_ids(m, 8.0, 2.2)
    m.hold("ParamEyeLOpen", 0.06)
    m.hold("ParamEyeROpen", 0.06)
    return m


def idle_devil() -> Motion:
    m = Motion(6.0, loop=True)
    m.hold("ParamBrowLForm", 0.7)
    m.hold("ParamBrowRForm", -0.4)
    m.hold("ParamMouthForm", 1.1)
    m.keys("ParamEyeLOpen", [(0, 0.88), (6.0, 0.88)])
    m.keys("ParamEyeROpen", [(0, 0.88), (6.0, 0.88)])
    m.waves("ParamBodyAngleY", 0, 3.5, 1.5)
    m.waves("ParamBodyAngleZ", 0, 3.0, 2.0)  # bob (visible channel)
    m.waves("ParamAngleZ", 0, 5.0, 3.0)  # asymmetric head-tilt smirk
    m.fn("ParamEyeBallX", lambda t: 0.8 * max(0.0, math.sin(TAU * t / 6.0)) ** 3)  # one sly sweep per loop
    m.keys("ParamMouthForm", [(0, 1.1), (4.2, 1.1, linear), (4.5, 1.4, ease_out), (5.2, 1.1, ease_in), (6.0, 1.1, linear)])  # smirk deepens
    m.waves("Param73", 0.55, 0.45, 1.5)
    # fast tail-tip flicks on the 7-segment xk rig
    for i in range(1, 8):
        m.fn(f"Param_Angle_Rotation_{i}_xk4",
             (lambda i: lambda t: 22.0 * math.sin(TAU * t / 1.2 - i * 0.9))(i))
    return m


def happy_motion() -> Motion:
    # the generic 开心 act: open-mouth laugh, wiggle, wink, hearts
    m = Motion(1.8)
    m.hold("love", 1.0)
    m.keys("Param9", [(0, 0), (0.3, 1.0, ease_out), (1.5, 1.0, linear), (1.8, 0, smooth)])
    m.sweep("aixing", 0, 14)
    m.keys("ParamBodyAngleZ", [(0, 0), (0.35, 7, smooth), (0.75, -6, smooth), (1.15, 6, smooth), (1.5, 0, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.4, 10, smooth), (1.0, -8, smooth), (1.5, 0, smooth)])
    m.keys("Paramhh2", [(0, 0), (0.3, 1, smooth), (1.4, 1, linear), (1.8, 0, smooth)])  # blush joy
    m.hold("ParamMouthForm", 1.0)
    m.keys("ParamEyeROpen", [(0, 1), (0.9, 1, linear), (1.1, -1, ease_in), (1.8, 1, ease_out)])
    m.keys("ParamBrowLY", [(0, 0), (0.3, -0.7, smooth), (1.4, -0.7, linear), (1.8, 0, smooth)])
    m.keys("ParamBrowRY", [(0, 0), (0.3, -0.7, smooth), (1.4, -0.7, linear), (1.8, 0, smooth)])
    add_tail_ids(m, 30.0, 0.7)
    return m


def idle_music() -> Motion:
    # 120 BPM groove: body sways per beat, head nods, humming mouth, tail off-beat
    m = Motion(6.0, loop=True)
    m.waves("ParamBodyAngleZ", 0, 4.5, 1.0)      # two-beat sway
    m.waves("ParamAngleZ", 0, 6.0, 2.0)          # slow groove tilt
    m.waves("ParamAngleY", -3.0, 2.5, 1.0)       # nod on the beat
    m.waves("ParamBodyAngleY", 0, 2.0, 0.5)      # beat bounce
    m.fn("ParamMouthOpenY", lambda t: 0.12 + 0.14 * max(0.0, math.sin(TAU * t / 1.0)) ** 2)  # hum-hum
    m.hold("ParamMouthForm", 0.6)
    # enjoying with eyes closed; one full 0.9s open-peek per loop
    m.keys("ParamEyeLOpen", [(0, 0.12), (2.4, 0.12, linear), (2.52, 1.0, ease_out), (3.3, 1.0, linear), (3.42, 0.12, ease_in), (6.0, 0.12, linear)])
    m.keys("ParamEyeROpen", [(0, 0.12), (2.4, 0.12, linear), (2.52, 1.0, ease_out), (3.3, 1.0, linear), (3.42, 0.12, ease_in), (6.0, 0.12, linear)])
    m.waves("Param73", 0.55, 0.45, 1.2)  # star twinkle with the beat
    m.fn("ParamBreath", breath(period=3.0, amp=0.4, center=0.5))
    add_tail_ids(m, 12.0, 2.0)
    return m


def d_mischief() -> Motion:
    m = Motion(2.0)
    m.waves("ParamBodyAngleY", 0, 4.5, 0.35)
    m.keys("ParamMouthForm", [(0, 1.1), (0.4, 1.4, smooth), (1.6, 1.2, smooth), (2.0, 1.1, smooth)])
    m.keys("Param73", [(0, 0.55), (0.5, 1.0, ease_out), (0.8, 0.5, smooth), (1.2, 1.0, ease_out), (1.6, 0.55, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.6, 6, smooth), (1.5, 4, smooth), (2.0, 0, smooth)])
    m.keys("ParamCheek21", [(0, 0), (0.35, 1, smooth), (1.5, 1, linear), (2.0, 0, smooth)])  # tongue
    m.keys("ParamEyeROpen", [(0, 1), (0.5, -1, smooth), (1.4, -1, linear), (2.0, 1, smooth)])  # wink
    for i in range(1, 8):
        m.fn(f"Param_Angle_Rotation_{i}_xk4",
             (lambda i: lambda t: 28.0 * math.sin(TAU * t / 0.6 - i * 0.9))(i))
    return m


def d_zap() -> Motion:
    # shiver + exclamation flash + wide eyes
    m = Motion(1.5)
    m.fn("ParamAngleX", lambda t: 8.0 * math.sin(TAU * t / 0.14) * math.exp(-2.0 * t))
    m.fn("ParamAngleZ", lambda t: 5.0 * math.sin(TAU * t / 0.14 + 1.1) * math.exp(-2.0 * t))
    m.keys("ParamCheek75", [(0, 0), (0.1, 1, ease_out), (1.0, 1, linear), (1.4, 0, smooth)])
    m.keys("ParamBrowLY", [(0, 0), (0.12, 0.8, ease_out), (1.0, 0.8, linear), (1.5, 0, smooth)])
    m.keys("ParamBrowRY", [(0, 0), (0.12, 0.8, ease_out), (1.0, 0.8, linear), (1.5, 0, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.15, 0.85, ease_out), (0.9, 0.5, smooth), (1.5, 0, smooth)])
    add_tail_ids(m, 30.0, 0.4)
    return m


def d_smug() -> Motion:
    # 调皮 recipe: wink + tongue-lick mouth + tilted brow + chin-up
    m = Motion(1.8)
    m.keys("ParamCheek21", [(0, 0), (0.3, 1, smooth), (1.4, 1, linear), (1.8, 0, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.3, -1, smooth), (1.4, -1, linear), (1.8, 1, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.3, 1.5, smooth), (1.4, 1.5, linear), (1.8, -0.5, smooth)])
    m.keys("ParamBrowLAngle", [(0, 0), (0.3, -0.8, smooth), (1.4, -0.8, linear), (1.8, 0, smooth)])
    m.keys("ParamAngleZ", [(0, 0), (0.4, 12, smooth), (1.4, 12, linear), (1.8, 0, smooth)])
    add_tail_ids(m, 20.0, 0.8)
    return m


def d_laugh() -> Motion:
    m = Motion(1.8)
    m.keys("ParamBodyAngleZ", [(0, 0), (0.35, 6, smooth), (0.75, -5, smooth), (1.15, 5, smooth), (1.55, -3, smooth), (1.8, 0, smooth)])
    m.keys("ji", [(0, 0), (0.4, 0.6, ease_out), (0.8, 0, smooth), (1.2, 0.5, ease_out), (1.6, 0, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.3, 0.65, ease_out), (0.6, 0.08, smooth), (0.95, 0.6, ease_out), (1.3, 0.08, smooth), (1.65, 0.55, ease_out), (1.8, 0, smooth)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.3, 0.25, smooth), (0.6, 1, smooth), (0.95, 0.25, smooth), (1.3, 1, smooth), (1.65, 0.25, smooth), (1.8, 1, smooth)])
    m.keys("ParamEyeROpen", [(0, 1), (0.3, 0.25, smooth), (0.6, 1, smooth), (0.95, 0.25, smooth), (1.3, 1, smooth), (1.65, 0.25, smooth), (1.8, 1, smooth)])
    m.keys("Param73", [(0, 0.55), (0.4, 1.0, ease_out), (1.5, 1.0, linear), (1.8, 0.55, smooth)])
    for i in range(1, 8):
        m.fn(f"Param_Angle_Rotation_{i}_xk4",
             (lambda i: lambda t: 25.0 * math.sin(TAU * t / 0.5 - i * 0.9))(i))
    return m


def d_wingpanic() -> Motion:
    # rig has no visible wings: panic = rapid head shake + worried face
    m = Motion(2.0)
    m.fn("ParamAngleZ", lambda t: 18.0 * math.sin(TAU * t / 0.24) * math.exp(-0.6 * t))
    m.keys("ParamCheek19", [(0, 0), (0.15, 1, ease_out), (1.6, 1, linear), (2.0, 0, smooth)])
    m.fn("ParamBodyAngleZ", lambda t: 6.0 * math.sin(TAU * t / 0.24 + 0.7) * math.exp(-1.2 * t))
    m.keys("ParamBrowLY", [(0, 0), (0.2, -0.7, smooth), (1.4, -0.7, linear), (2.0, 0, smooth)])
    m.keys("ParamBrowRY", [(0, 0), (0.2, -0.7, smooth), (1.4, -0.7, linear), (2.0, 0, smooth)])
    m.keys("ParamMouthForm", [(0, -0.5), (0.2, -1.4, smooth), (1.4, -1.4, linear), (2.0, -0.5, smooth)])
    m.keys("ParamMouthOpenY", [(0, 0), (0.3, 0.5, smooth), (0.9, 0.15, smooth), (1.2, 0.45, smooth), (1.6, 0, smooth)])
    add_tail_ids(m, 36.0, 0.35)
    return m


def idle_default() -> Motion:
    # Calm daily presence: breathing, two full blinks at uneven phases,
    # drifting gaze and a soft head sway.
    m = Motion(6.8, loop=True)
    m.fn("ParamBreath", breath(period=3.4, amp=0.45, center=0.5))
    pts = [(0, 1.0)]
    for t in (1.9, 4.7):
        pts += [(t - 0.06, 1.0, linear), (t, 0.05, ease_in), (t + 0.1, 1.0, ease_out)]
    pts.append((6.8, 1.0, linear))
    m.keys("ParamEyeLOpen", pts)
    m.keys("ParamEyeROpen", pts)
    m.waves("ParamEyeBallX", 0, 0.5, 6.8)
    m.waves("ParamEyeBallY", 0.1, 0.25, 3.4)
    m.waves("ParamAngleZ", 0, 2.0, 6.8)
    m.fn("ParamAngleY", lambda t: -3.0 + 1.5 * math.sin(TAU * t / 3.4))
    add_tail_ids(m, 18.0, 1.7)
    return m


def headpat_work() -> Motion:
    # typing startle: squeeze eyes, jolt up, settle back to the -6 study tilt
    m = Motion(2.4)
    m.keys("ParamAngleY", [(0, -6), (0.15, -2, ease_out), (0.5, -9, ease_in), (1.2, -7, smooth), (2.4, -6, linear)])
    m.keys("ParamBodyAngleY", [(0, 0), (0.3, 1.5, ease_out), (1.0, 0, smooth), (2.4, 0, linear)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.15, 0.1, ease_out), (0.9, 0.3, smooth), (1.6, 1, ease_in), (2.4, 1, linear)])
    m.keys("ParamEyeROpen", [(0, 1), (0.15, 0.1, ease_out), (0.9, 0.3, smooth), (1.6, 1, ease_in), (2.4, 1, linear)])
    m.keys("ParamMouthForm", [(0, 0), (0.2, 0.6, ease_out), (1.0, 0.3, smooth), (2.0, 0, smooth), (2.4, 0, linear)])
    m.hold("ParamCheek70", 1.0)
    add_tail_ids(m, 12.0, 1.2)
    return m


def headpat_music() -> Motion:
    # nod along to the beat three times, eyes closed, humming smile
    m = Motion(2.6)
    m.keys("ParamAngleY", [(0, 0), (0.35, 9, ease_out), (0.8, 4, smooth), (1.15, 8, ease_out), (1.6, 3, smooth), (1.95, 7, ease_out), (2.3, 0, smooth), (2.6, 0, linear)])
    m.keys("ParamBodyAngleZ", [(0, 0), (0.5, 3, ease_out), (1.3, -3, smooth), (2.0, 0, smooth), (2.6, 0, linear)])
    m.keys("ParamEyeLOpen", [(0, 1), (0.3, 0.15, ease_out), (2.2, 0.15, linear), (2.45, 1, ease_in), (2.6, 1, linear)])
    m.keys("ParamEyeROpen", [(0, 1), (0.3, 0.15, ease_out), (2.2, 0.15, linear), (2.45, 1, ease_in), (2.6, 1, linear)])
    m.hold("ParamMouthForm", 0.8)
    add_tail_ids(m, 12.0, 1.0)
    return m


def headpat_meal() -> Motion:
    # protective munch: cheeks full, wary narrow eyes, slight guarding turn
    m = Motion(2.2)
    m.keys("ParamMouthOpenY", [(0, 0.5), (0.3, 0.75, ease_out), (1.2, 0.6, linear), (1.8, 0.2, smooth), (2.2, 0, smooth)])
    m.keys("ParamMouthForm", [(0, 0.3), (0.4, -0.4, ease_out), (1.5, -0.4, linear), (2.0, 0, smooth), (2.2, 0, linear)])
    m.keys("ParamEyeLOpen", [(0, 0.8), (0.25, 0.45, ease_out), (1.4, 0.45, linear), (1.9, 0.8, ease_in), (2.2, 0.8, linear)])
    m.keys("ParamEyeROpen", [(0, 0.8), (0.25, 0.45, ease_out), (1.4, 0.45, linear), (1.9, 0.8, ease_in), (2.2, 0.8, linear)])
    m.keys("ParamAngleY", [(0, 0), (0.3, 6, ease_out), (1.2, 5, linear), (2.0, 0, smooth), (2.2, 0, linear)])
    m.hold("danbaofan", 1.0)
    m.hold("point", 0.0)
    add_tail_ids(m, 12.0, 1.4)
    return m


def idle_morning() -> Motion:
    # nodding off and half waking up, over and over
    m = Motion(8.0, loop=True)
    m.fn("ParamBreath", breath(period=4.0, amp=0.5, center=0.5))
    m.fn("ParamAngleY", lambda t: -3.0 - 5.0 * max(0.0, math.sin(TAU * t / 8.0)) ** 2)
    m.waves("ParamBodyAngleZ", 0, 1.5, 4.0)
    m.fn("ParamEyeLOpen", lambda t: max(0.08, 0.35 - 0.3 * max(0.0, math.sin(TAU * t / 8.0))))
    m.fn("ParamEyeROpen", lambda t: max(0.08, 0.35 - 0.3 * max(0.0, math.sin(TAU * t / 8.0))))
    m.waves("ParamEyeBallX", 0, 0.3, 8.0)
    add_tail_ids(m, 10.0, 2.0)
    return m


def idle_drama() -> Motion:
    # eyes glued to the screen, snack nibbles, body slouched in
    m = Motion(6.0, loop=True)
    m.waves("ParamBodyAngleX", 0, 1.5, 6.0)
    m.waves("ParamBodyAngleZ", 0, 1.2, 3.0)
    m.fn("ParamEyeBallX", lambda t: 0.55 * math.sin(TAU * t / 1.2) + 0.25 * math.sin(TAU * t / 3.0))
    m.fn("ParamEyeLOpen", lambda t: 0.9 - 0.25 * max(0.0, math.sin(TAU * t / 6.0)) ** 3)
    m.fn("ParamEyeROpen", lambda t: 0.9 - 0.25 * max(0.0, math.sin(TAU * t / 6.0)) ** 3)
    m.fn("ParamMouthOpenY", lambda t: 0.18 + 0.15 * max(0.0, math.sin(TAU * t / 1.5)) ** 2)
    m.fn("ParamBreath", breath(period=3.0, amp=0.4, center=0.5))
    add_tail_ids(m, 14.0, 2.0)
    return m


# registry: name -> (builder, fade_in, fade_out, is_idle)
MOTIONS: dict[str, tuple] = {
    "poke": (poke, 0.15, 0.25, False),
    "shy": (shy, 0.15, 0.3, False),
    "surprised": (surprised, 0.1, 0.2, False),
    "headpat": (headpat, 0.2, 0.3, False),
    "headpat_work": (headpat_work, 0.2, 0.3, False),
    "headpat_music": (headpat_music, 0.2, 0.3, False),
    "headpat_meal": (headpat_meal, 0.2, 0.3, False),
    "d_wave": (d_wave, 0.25, 0.35, False),
    "d_look": (d_look, 0.25, 0.35, False),
    "d_stretch": (d_stretch, 0.3, 0.4, False),
    "d_ear": (d_ear, 0.2, 0.3, False),
    "idle_default": (idle_default, 0.8, 0.8, True),
    "idle_work": (idle_work, 0.8, 0.8, True),
    "w_type": (w_type, 0.25, 0.35, False),
    "w_think": (w_think, 0.3, 0.4, False),
    "w_sip": (w_sip, 0.3, 0.4, False),
    "w_stretch": (w_stretch, 0.3, 0.4, False),
    "w_cheer": (w_cheer, 0.25, 0.35, False),
    "idle_meal": (idle_meal, 0.8, 0.8, True),
    "m_bite": (m_bite, 0.3, 0.4, False),
    "m_parfait": (m_parfait, 0.3, 0.4, False),
    "m_gulp": (m_gulp, 0.3, 0.4, False),
    "m_protect": (m_protect, 0.25, 0.35, False),
    "m_yummy": (m_yummy, 0.25, 0.35, False),
    "idle_clingy": (idle_clingy, 0.8, 0.8, True),
    "c_bap": (c_bap, 0.2, 0.3, False),
    "c_rub": (c_rub, 0.3, 0.4, False),
    "c_purr": (c_purr, 0.3, 0.4, False),
    "c_dangle": (c_dangle, 0.2, 0.3, False),
    "c_nuzzle": (c_nuzzle, 0.3, 0.4, False),
    "idle_happy": (idle_happy, 0.8, 0.8, True),
    "h_jump": (h_jump, 0.25, 0.35, False),
    "h_spin": (h_spin, 0.3, 0.4, False),
    "h_glee": (h_glee, 0.25, 0.35, False),
    "h_boing": (h_boing, 0.2, 0.3, False),
    "h_loveburst": (h_loveburst, 0.25, 0.35, False),
    "idle_night": (idle_night, 1.0, 1.0, True),
    "n_wakeup": (n_wakeup, 0.2, 0.3, False),
    "n_drowse": (n_drowse, 0.3, 0.4, False),
    "n_float": (n_float, 0.4, 0.5, False),
    "n_dream": (n_dream, 0.3, 0.4, False),
    "idle_morning": (idle_morning, 1.0, 1.0, True),
    "idle_drama": (idle_drama, 0.8, 0.8, True),
    "idle_devil": (idle_devil, 0.8, 0.8, True),
    "happy": (happy_motion, 0.25, 0.35, False),
    "idle_music": (idle_music, 0.8, 0.8, True),
    "d_mischief": (d_mischief, 0.25, 0.35, False),
    "d_zap": (d_zap, 0.25, 0.35, False),
    "d_smug": (d_smug, 0.3, 0.4, False),
    "d_laugh": (d_laugh, 0.25, 0.35, False),
    "d_wingpanic": (d_wingpanic, 0.2, 0.3, False),
}


def load_cdi_ids() -> set:
    with open(CDI_PATH, encoding="utf-8") as fh:
        cdi = json.load(fh)
    return {p["Id"] for p in cdi.get("Parameters", [])}


def validate_all(ids: set) -> list[str]:
    errors = []
    for name, (builder, _fi, _fo, _idle) in MOTIONS.items():
        try:
            motion = builder()
            data = motion.to_json()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        for curve in data["Curves"]:
            if curve["Id"] not in ids:
                errors.append(f"{name}: unknown parameter id {curve['Id']}")
    return errors


def register_model3() -> None:
    with open(MODEL3_PATH, encoding="utf-8") as fh:
        model3 = json.load(fh)
    motions = model3.setdefault("FileReferences", {}).setdefault("Motions", {})
    added = []
    for name, (_builder, fade_in, fade_out, _idle) in MOTIONS.items():
        if name in motions:
            continue
        motions[name] = [{
            "File": f"motions/{name}.motion3.json",
            "FadeInTime": fade_in,
            "FadeOutTime": fade_out,
        }]
        added.append(name)
    with open(MODEL3_PATH, "w", encoding="utf-8") as fh:
        json.dump(model3, fh, ensure_ascii=False, indent=2)
    print(f"registered {len(added)} motion groups: {', '.join(added)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    parser.add_argument("--out", default=os.path.join(MODEL_DIR, "motions"))
    parser.add_argument("--register", action="store_true", help="also register groups in c_0120.model3.json")
    args = parser.parse_args()

    ids = load_cdi_ids()
    errors = validate_all(ids)
    if errors:
        for error in errors:
            print("ERROR", error)
        raise SystemExit(f"{len(errors)} validation error(s)")
    print(f"validated {len(MOTIONS)} motions against cdi3 ids")

    if args.check:
        return
    os.makedirs(args.out, exist_ok=True)
    for name, (builder, _fi, _fo, _idle) in MOTIONS.items():
        data = builder().to_json()
        path = os.path.join(args.out, f"{name}.motion3.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        meta = data["Meta"]
        print(f"  {name}: {meta['Duration']}s loop={meta['Loop']} curves={meta['CurveCount']} points={meta['TotalPointCount']}")
    if args.register:
        register_model3()


if __name__ == "__main__":
    main()

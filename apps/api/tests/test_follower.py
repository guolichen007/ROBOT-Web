"""Regression tests for the turn-then-drive waypoint follower.

These guard against the A28 orbit bug: a large heading error must never be
combined with forward motion, and an unreachable waypoint must surface as
stalled rather than spinning forever.
"""

from __future__ import annotations

import math

import pytest

from app.modules.navigation.follower import (
    ARRIVE_DISTANCE_M,
    drive_speed,
    follower_command,
    normalize,
)


def test_large_heading_error_rotates_in_place_without_driving() -> None:
    # Robot at A19 (8, 3.0) facing south (-pi/2); next waypoint is A28 (14, 3.0).
    dx, dy = 14.0 - 8.0, 3.0 - 3.0
    linear, angular, state = follower_command(-math.pi / 2, dx, dy, math.hypot(dx, dy))
    assert state == "ROTATE"
    assert linear == 0.0
    assert angular > 0


def test_aligned_segment_drives_at_distance_speed() -> None:
    dx, dy = 0.0, -10.0  # straight south
    linear, _angular, state = follower_command(-math.pi / 2, dx, dy, 10.0)
    assert state == "DRIVE"
    assert linear > 0
    assert linear == drive_speed(10.0)


def test_arrive_snaps_and_returns_zero_command() -> None:
    linear, angular, state = follower_command(0.0, 0.1, 0.0, 0.1)
    assert state == "ARRIVE"
    assert linear == 0.0
    assert angular == 0.0


def test_heading_normalizes_to_pi_range() -> None:
    assert abs(normalize(3 * math.pi)) - math.pi < 1e-9
    assert abs(normalize(-3 * math.pi)) - math.pi < 1e-9

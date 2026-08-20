"""Pure turn-then-drive waypoint follower control law.

The mock robot uses this to follow a waypoint list without orbiting a target:
when the heading error is large the vehicle stops and turns in place; only
after converging does it drive. This is a pure function so it can be unit
tested independently of MQTT.
"""

from __future__ import annotations

import math

ARRIVE_DISTANCE_M = 0.25
HEADING_ALIGN_RAD = 0.14  # ~8 degrees

# State-aware watchdog timeouts. A 180-degree turn at max angular speed
# (0.8 rad/s) takes ~3.9 s, during which distance-to-target does not shrink;
# a single distance-based stall timeout would falsely fail the follower.
ROTATE_STALL_TIMEOUT_S = 8.0
DRIVE_STALL_TIMEOUT_S = 4.0
HEADING_PROGRESS_EPSILON = 0.02
DISTANCE_PROGRESS_EPSILON = 0.02


def normalize(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def drive_speed(distance: float) -> float:
    if distance > 3.0:
        return 2.8
    if distance > 1.0:
        return 1.6
    return 0.6


def follower_command(
    theta: float, dx: float, dy: float, distance: float
) -> tuple[float, float, str, float]:
    """Return (linear, angular, state, heading_error) for one tick.

    state is one of ARRIVE / ROTATE / DRIVE. The caller must snap position and
    advance the waypoint index on ARRIVE. heading_error is the normalized
    error in [-pi, pi], used by the state-aware watchdog.
    """
    if distance < ARRIVE_DISTANCE_M:
        return (0.0, 0.0, "ARRIVE", 0.0)
    desired = math.atan2(dy, dx) if distance > 1e-6 else theta
    heading_error = normalize(desired - theta)
    if abs(heading_error) > HEADING_ALIGN_RAD:
        return (0.0, _clamp(heading_error * 3.0, -0.8, 0.8), "ROTATE", heading_error)
    return (drive_speed(distance), _clamp(heading_error * 2.0, -0.5, 0.5), "DRIVE", heading_error)

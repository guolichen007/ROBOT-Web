// Unified robot body frame (ROS convention).
//
//   local +X = forward
//   local +Y = left
//   local -Y = right
//
// World frame (matches MapAdapter): +X = east, +Y = north, theta CCW from +X.

export interface Pose {
  x: number
  y: number
  theta: number
}

export interface Vec2 {
  x: number
  y: number
}

/** Robot forward unit vector in world frame. */
export function vehicleForwardVector(theta: number): Vec2 {
  return { x: Math.cos(theta), y: Math.sin(theta) }
}

/** Robot right-side unit vector in world frame (right = local -Y). */
export function vehicleRightVector(theta: number): Vec2 {
  return { x: Math.sin(theta), y: -Math.cos(theta) }
}

/** Transform a body-frame point (lx, ly) into world coordinates. */
export function bodyToWorld(pose: Pose, lx: number, ly: number): Vec2 {
  const c = Math.cos(pose.theta)
  const s = Math.sin(pose.theta)
  return { x: pose.x + lx * c - ly * s, y: pose.y + lx * s + ly * c }
}

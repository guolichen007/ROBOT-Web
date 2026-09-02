import type { RobotState, SensorProfile } from '@/types'

export interface XY {
  x: number
  y: number
}

export const RIGHT_SENSOR_ORIENTATION_INVALID = 'RIGHT_SENSOR_ORIENTATION_INVALID'

export function rightSensorProfile(robot?: RobotState): SensorProfile | undefined {
  return robot?.sensor_profiles?.find((p) => p.nominal_side?.toUpperCase() === 'RIGHT')
}

/**
 * Builds the visual detection sector polygon (world coordinates) for the
 * vehicle's right-side sensor. Returns [] when pose or profile is missing.
 */
export function buildSensorSector(
  robot: Pick<RobotState, 'x' | 'y' | 'theta'>,
  profile: SensorProfile,
  segments = 22,
): XY[] {
  if (robot.x == null || robot.y == null || robot.theta == null) return []
  const c = Math.cos(robot.theta)
  const s = Math.sin(robot.theta)
  const ox = robot.x + c * profile.sensor_mount_x_m - s * profile.sensor_mount_y_m
  const oy = robot.y + s * profile.sensor_mount_x_m + c * profile.sensor_mount_y_m
  const yaw = robot.theta + profile.sensor_mount_yaw_rad
  const points: XY[] = [{ x: ox, y: oy }]
  for (let i = 0; i <= segments; i++) {
    const a = yaw - profile.coverage_fov_rad / 2 + (profile.coverage_fov_rad * i) / segments
    points.push({
      x: ox + Math.cos(a) * profile.coverage_range_m,
      y: oy + Math.sin(a) * profile.coverage_range_m,
    })
  }
  return points
}

/**
 * Verifies the sector's centroid actually lies in the vehicle's local right
 * half-plane (world XY, theta CCW from +X). A mis-configured mount must be
 * surfaced as invalid rather than mirrored into a fake "correct" side.
 */
export function isSectorOnVehicleRight(robot: Pick<RobotState, 'x' | 'y' | 'theta'>, sector: XY[]): boolean {
  if (robot.x == null || robot.y == null || robot.theta == null || sector.length < 3) return false
  const arc = sector.slice(1)
  const centroid = arc.reduce((acc, p) => ({ x: acc.x + p.x, y: acc.y + p.y }), { x: 0, y: 0 })
  centroid.x /= arc.length
  centroid.y /= arc.length
  const right = { x: Math.sin(robot.theta), y: -Math.cos(robot.theta) }
  const rel = { x: centroid.x - robot.x, y: centroid.y - robot.y }
  return rel.x * right.x + rel.y * right.y > 0
}

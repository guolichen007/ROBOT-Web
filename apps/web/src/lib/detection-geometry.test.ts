import { describe, expect, it } from 'vitest'
import { buildSensorSector, isSectorOnVehicleRight, rightSensorProfile } from './detection-geometry'
import type { RobotState, SensorProfile } from '@/types'

const RIGHT: SensorProfile = {
  channel: 'right_fire',
  support_state: 'CONNECTED',
  nominal_side: 'RIGHT',
  sensor_mount_x_m: 0.3,
  sensor_mount_y_m: 0.2,
  sensor_mount_yaw_rad: -Math.PI / 2, // mounted facing the vehicle's right (clockwise)
  coverage_range_m: 5,
  coverage_fov_rad: Math.PI / 3,
}

function robot(theta: number): RobotState {
  return { vehicle_id: 'R001', x: 4, y: 4, theta }
}

describe('rightSensorProfile', () => {
  it('picks the RIGHT profile regardless of case', () => {
    const candidate: RobotState = {
      vehicle_id: 'R001',
      sensor_profiles: [
        { ...RIGHT, nominal_side: 'LEFT' },
        { ...RIGHT, nominal_side: 'right' },
      ],
    }
    expect(rightSensorProfile(candidate)?.nominal_side).toBe('right')
  })

  it('returns undefined when no RIGHT profile exists', () => {
    expect(rightSensorProfile({ vehicle_id: 'R001' })).toBeUndefined()
  })
})

describe('buildSensorSector + right-side guard', () => {
  it.each([
    [0, 'east'],
    [Math.PI / 2, 'north'],
    [Math.PI, 'west'],
    [-Math.PI / 2, 'south'],
  ])('theta=%s (%s) keeps the sector centroid on the vehicle right half-plane', (theta) => {
    const sector = buildSensorSector(robot(theta), RIGHT)
    expect(sector.length).toBeGreaterThan(3)
    expect(isSectorOnVehicleRight(robot(theta), sector)).toBe(true)
  })

  it('flags a mis-mounted RIGHT sensor (facing left) as invalid', () => {
    const bad: SensorProfile = { ...RIGHT, sensor_mount_yaw_rad: Math.PI / 2 }
    const sector = buildSensorSector(robot(0), bad)
    expect(isSectorOnVehicleRight(robot(0), sector)).toBe(false)
  })

  it('returns an empty sector when pose is missing', () => {
    expect(buildSensorSector({}, RIGHT)).toEqual([])
  })
})

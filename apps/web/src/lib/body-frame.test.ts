import { describe, expect, it } from 'vitest'
import { bodyToWorld, vehicleForwardVector, vehicleRightVector } from './body-frame'

describe('robot body frame', () => {
  it('forward and right are perpendicular for the four cardinal headings', () => {
    for (const theta of [0, Math.PI / 2, Math.PI, -Math.PI / 2]) {
      const forward = vehicleForwardVector(theta)
      const right = vehicleRightVector(theta)
      expect(forward.x * right.x + forward.y * right.y).toBeCloseTo(0, 10)
    }
  })

  it('theta=0: front east, right south', () => {
    expect(vehicleForwardVector(0).x).toBeCloseTo(1, 10)
    expect(vehicleForwardVector(0).y).toBeCloseTo(0, 10)
    expect(vehicleRightVector(0).x).toBeCloseTo(0, 10)
    expect(vehicleRightVector(0).y).toBeCloseTo(-1, 10)
  })

  it('theta=PI/2: front north, right east', () => {
    expect(vehicleForwardVector(Math.PI / 2).y).toBeCloseTo(1, 10)
    expect(vehicleRightVector(Math.PI / 2).x).toBeCloseTo(1, 10)
  })

  it('theta=PI: front west, right north', () => {
    expect(vehicleForwardVector(Math.PI).x).toBeCloseTo(-1, 10)
    expect(vehicleRightVector(Math.PI).y).toBeCloseTo(1, 10)
  })

  it('theta=-PI/2: front south, right west', () => {
    expect(vehicleForwardVector(-Math.PI / 2).y).toBeCloseTo(-1, 10)
    expect(vehicleRightVector(-Math.PI / 2).x).toBeCloseTo(-1, 10)
  })

  it('bodyToWorld places the right-side mount on the vehicle right for all headings', () => {
    const pose = { x: 4, y: 4, theta: 0 }
    for (const theta of [0, Math.PI / 2, Math.PI, -Math.PI / 2]) {
      const current = { ...pose, theta }
      const mount = bodyToWorld(current, 0.35, -0.32)
      const rel = { x: mount.x - current.x, y: mount.y - current.y }
      const right = vehicleRightVector(theta)
      expect(rel.x * right.x + rel.y * right.y).toBeGreaterThan(0)
    }
  })
})

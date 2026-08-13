import { describe, expect, it } from 'vitest'
import { MapAdapter } from '@/lib/map-adapter'

describe('MapAdapter', () => {
  const adapter = new MapAdapter(
    { width_m: 30, height_m: 20, origin_x: 0, origin_y: 0, rotation_rad: 0 },
    { width: 900, height: 600 },
  )

  it('flips screen Y while preserving world coordinates', () => {
    const bottom = adapter.worldToScreen({ x: 2, y: 2 })
    const top = adapter.worldToScreen({ x: 2, y: 18 })
    expect(top.y).toBeLessThan(bottom.y)
  })

  it('round trips a world coordinate', () => {
    const source = { x: 14.25, y: 8.75 }
    const result = adapter.screenToWorld(adapter.worldToScreen(source))
    expect(result.x).toBeCloseTo(source.x, 6)
    expect(result.y).toBeCloseTo(source.y, 6)
  })

  it('round trips coordinates with map rotation', () => {
    const rotated = new MapAdapter(
      { width_m: 20, height_m: 20, origin_x: 10, origin_y: 10, rotation_rad: Math.PI / 2 },
      { width: 600, height: 600 },
    )
    const result = rotated.screenToWorld(rotated.worldToScreen({ x: 12, y: 15 }))
    expect(result.x).toBeCloseTo(12, 6)
    expect(result.y).toBeCloseTo(15, 6)
  })

  it('keeps pan and zoom until explicitly reset', () => {
    adapter.setZoom(1.8)
    adapter.panBy(30, -12)
    expect(adapter.getZoom()).toBe(1.8)
    expect(adapter.getPan()).toEqual({ x: 30, y: -12 })
    adapter.reset()
    expect(adapter.getZoom()).toBe(1)
    expect(adapter.getPan()).toEqual({ x: 0, y: 0 })
  })
})

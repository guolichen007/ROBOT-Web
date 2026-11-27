import { describe, expect, it } from 'vitest'
import { SceneEditError, SceneEditor, makePose, type SceneState } from '@/lib/scene-editor'

function blank(status: SceneState['status'] = 'DRAFT'): SceneState {
  return {
    scene_id: 'a1000000-0000-4000-8000-000000000001',
    scene_version: 2,
    base_scene_version: 1,
    status,
    site_code: 'SIM_GAZEBO',
    nav_map: {
      map_code: 'SIM_GAZEBO_R002',
      map_version: 'sim-1',
      frame_id: 'map',
      resolution_m_per_px: 0.05,
      origin: makePose(-10, -10, 0),
      width_px: 400,
      height_px: 400,
      image_sha256: '1'.repeat(64),
      yaml_sha256: '2'.repeat(64),
      map_checksum: 'sim-gazebo-r002-v1',
      occupied_thresh: 0.65,
      free_thresh: 0.196,
      negate: 0,
    },
    points: [],
    parking_slots: [],
    routes: [],
    corridors: [],
  }
}

function code(fn: () => unknown): string {
  try {
    fn()
  } catch (e) {
    return (e as SceneEditError).code
  }
  return 'NO_ERROR'
}

const live = (x: number, y: number, yaw: number, over: Record<string, unknown> = {}) => ({
  quality: 'LIVE' as const,
  value: makePose(x, y, yaw),
  localization: 'OK',
  map_code: 'SIM_GAZEBO_R002',
  map_version: 'sim-1',
  ...over,
})

describe('SceneEditor CRUD + undo/redo', () => {
  it('adds, drags, rotates, precise-edits and undoes step by step', () => {
    const ed = new SceneEditor(blank())
    const id = ed.addPoint({ kind: 'INSPECTION', code: 'INSP-01', pose: makePose(1, 2, 0) })
    ed.updatePoint(id, { x: 1.5, y: 2.5 }) // drag
    ed.updatePoint(id, { yaw: 4 }) // rotate, normalised into (-pi, pi]
    expect(ed.scene.points[0].pose.yaw).toBeCloseTo(4 - 2 * Math.PI, 9)
    ed.updatePoint(id, { x: 3.25 }) // precise input
    expect(ed.scene.points[0].pose.x).toBe(3.25)
    ed.undo()
    expect(ed.scene.points[0].pose.x).toBe(1.5)
    ed.undo()
    ed.undo()
    expect(ed.scene.points[0].pose).toMatchObject({ x: 1, y: 2, yaw: 0 })
    ed.redo()
    expect(ed.scene.points[0].pose.x).toBe(1.5)
    ed.undo()
    ed.undo()
    expect(ed.scene.points).toHaveLength(0)
    expect(ed.canUndo).toBe(false)
  })

  it('a failed mutation leaves state and history untouched', () => {
    const ed = new SceneEditor(blank())
    ed.addPoint({ kind: 'INSPECTION', code: 'A', pose: makePose(0, 0, 0) })
    expect(code(() => ed.addPoint({ kind: 'INSPECTION', code: 'A', pose: makePose(1, 1, 0) }))).toBe(
      'DUPLICATE_POINT_CODE',
    )
    expect(ed.scene.points).toHaveLength(1)
    ed.undo()
    expect(ed.scene.points).toHaveLength(0)
  })

  it('new edit after undo clears redo; dirty tracks saved revision', () => {
    const ed = new SceneEditor(blank())
    ed.addPoint({ kind: 'INSPECTION', code: 'A', pose: makePose(0, 0, 0) })
    ed.markSaved()
    expect(ed.dirty).toBe(false)
    ed.addPoint({ kind: 'INSPECTION', code: 'B', pose: makePose(1, 0, 0) })
    expect(ed.dirty).toBe(true)
    ed.undo()
    expect(ed.dirty).toBe(false)
    ed.addPoint({ kind: 'INSPECTION', code: 'C', pose: makePose(2, 0, 0) })
    expect(ed.canRedo).toBe(false)
  })

  it.each([
    ['NaN', Number.NaN, 'NON_FINITE_NUMBER'],
    ['Infinity', Number.POSITIVE_INFINITY, 'NON_FINITE_NUMBER'],
    ['absurd', 1e9, 'COORDINATE_OUT_OF_RANGE'],
  ])('rejects %s coordinates', (_, x, expected) => {
    const ed = new SceneEditor(blank())
    expect(
      code(() => ed.addPoint({ kind: 'INSPECTION', code: 'A', pose: { x, y: 0, yaw: 0, frame_id: 'map' } })),
    ).toBe(expected)
  })

  it('PUBLISHED scene is read-only', () => {
    const ed = new SceneEditor(blank('PUBLISHED'))
    expect(code(() => ed.addPoint({ kind: 'INSPECTION', code: 'A', pose: makePose(0, 0, 0) }))).toBe(
      'SCENE_NOT_DRAFT',
    )
  })
})

describe('references', () => {
  it('refuses deleting a point referenced by a route, and a slot used by a point', () => {
    const ed = new SceneEditor(blank())
    const slot = ed.addSlot(
      'S-01',
      [
        { x: 0, y: 0 },
        { x: 2, y: 0 },
        { x: 2, y: 2 },
      ],
      makePose(1, 1, 0),
    )
    const a = ed.addPoint({ kind: 'RETURN_HOME', code: 'HOME', pose: makePose(0, 0, 0) })
    const b = ed.addPoint({
      kind: 'INSPECTION',
      code: 'I1',
      pose: makePose(1, 0, 0),
      parking_slot_code: 'S-01',
    })
    ed.addRoute('R1', [a, b])
    expect(code(() => ed.deletePoint(b))).toBe('POINT_REFERENCED_BY_ROUTE')
    expect(code(() => ed.deleteSlot(slot))).toBe('SLOT_IN_USE')
    expect(code(() => ed.addRoute('R2', [a, 'missing']))).toBe('ROUTE_POINT_REF_MISSING')
    expect(
      code(() =>
        ed.addPoint({ kind: 'INSPECTION', code: 'I2', pose: makePose(0, 1, 0), parking_slot_code: 'NOPE' }),
      ),
    ).toBe('POINT_SLOT_REF_MISSING')
  })

  it('route ordering, reverse and direction preview', () => {
    const ed = new SceneEditor(blank())
    const p = [0, 1, 2].map((i) =>
      ed.addPoint({ kind: 'INSPECTION', code: `P${i}`, pose: makePose(i * 2, 0, 0) }),
    )
    const r = ed.addRoute('R1', [p[0], p[1]])
    ed.appendToRoute(r, p[2])
    expect(code(() => ed.appendToRoute(r, p[2]))).toBe('ROUTE_REPEATED_ADJACENT_POINT')
    ed.moveRouteRef(r, 2, 0)
    expect(ed.scene.routes[0].point_refs).toEqual([p[2], p[0], p[1]])
    ed.reverseRoute(r)
    const segs = ed.routeSegments(r)
    expect(segs).toHaveLength(2)
    expect(segs[0].heading).toBeCloseTo(Math.PI, 9) // P1 -> P0 points to -X
  })

  it('right-lane corridor lies on the right of travel direction', () => {
    const ed = new SceneEditor(blank())
    const c = ed.addCorridor(
      'LANE-1',
      [
        { x: 0, y: 0 },
        { x: 10, y: 0 },
      ],
      1.5,
    )
    const poly = ed.corridorPolygon(c)
    expect(Math.min(...poly.map((v) => v.y))).toBeCloseTo(-1.5, 9) // travelling +X, right = -Y
    expect(Math.max(...poly.map((v) => v.y))).toBeCloseTo(0, 9)
    expect(
      code(() =>
        ed.addCorridor(
          'L2',
          [
            { x: 0, y: 0 },
            { x: 1, y: 0 },
          ],
          0.1,
        ),
      ),
    ).toBe('CORRIDOR_WIDTH_OUT_OF_RANGE')
  })
})

describe('teach from current vehicle pose (当前位置示教)', () => {
  it('accepts only LIVE, localised pose on the same nav map', () => {
    const ed = new SceneEditor(blank())
    const id = ed.teachPoint('INSPECTION', 'T1', live(3.2, -1.5, 1.5708))
    expect(ed.scene.points.find((p) => p.point_id === id)).toMatchObject({
      taught_from: 'VEHICLE_POSE',
      pose: { x: 3.2, y: -1.5 },
    })
    expect(code(() => ed.teachPoint('INSPECTION', 'T2', { ...live(0, 0, 0), quality: 'STALE' }))).toBe(
      'POSE_NOT_LIVE',
    )
    expect(
      code(() => ed.teachPoint('INSPECTION', 'T3', { ...live(0, 0, 0), value: null, quality: 'UNKNOWN' })),
    ).toBe('POSE_NOT_LIVE')
    expect(code(() => ed.teachPoint('INSPECTION', 'T4', live(0, 0, 0, { localization: 'LOST' })))).toBe(
      'LOCALIZATION_NOT_OK',
    )
    expect(
      code(() =>
        ed.teachPoint('INSPECTION', 'T5', live(0, 0, 0, { map_code: 'DEMO_PARKING', map_version: '1' })),
      ),
    ).toBe('POSE_MAP_MISMATCH')
  })
})

describe('manifest export', () => {
  it('never claims traversability and flags missing return-home', () => {
    const ed = new SceneEditor(blank())
    ed.addPoint({ kind: 'INSPECTION', code: 'I1', pose: makePose(1, 0, 0) })
    const m = ed.toManifest('2026-11-27T09:00:00+08:00') as {
      validation: { traversability: string; references_ok: boolean; issues: { code: string }[] }
      digest: null
      contract_version: string
    }
    expect(m.contract_version).toBe('2.1.0-draft.1')
    expect(m.validation.traversability).toBe('PENDING_VERIFICATION')
    expect(m.validation.references_ok).toBe(false)
    expect(m.validation.issues.map((i) => i.code)).toContain('RETURN_HOME_POINT_MISSING')
    expect(m.digest).toBeNull()
  })
})

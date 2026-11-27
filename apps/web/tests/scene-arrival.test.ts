import { describe, expect, it, vi } from 'vitest'
import { saveScene, toEditorState, type ServerScene } from '@/lib/scene-api'
import { SceneEditor } from '@/lib/scene-editor'

// WEB-R1C R1C-3: every route waypoint's arrival survives load -> edit -> save. R1B rebuilt it as
// "last = STOP, others PASS_THROUGH", silently turning every intermediate STOP into PASS_THROUGH.
const { putMock } = vi.hoisted(() => ({ putMock: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: { put: putMock, get: vi.fn(), post: vi.fn() } }))

const id = (n: number) => `00000000-0000-4000-8000-00000000000${n}`
const server: ServerScene = {
  scene_id: id(9),
  map_code: 'SIM',
  version: 'sim-1',
  status: 'DRAFT',
  storage_version: 2,
  revision: 3,
  traversability: 'PENDING_VERIFICATION',
  nav_map: {
    resolution_m_per_px: 0.05,
    origin: { x: 0, y: 0, yaw: 0 },
    width_px: 40,
    height_px: 30,
    occupied_thresh: 0.65,
    free_thresh: 0.196,
    negate: 0,
    image_sha256: 'a'.repeat(64),
    yaml_sha256: 'b'.repeat(64),
    map_checksum: null,
    image_url: '/api/v1/scenes/x/nav-image',
  },
  points: [1, 2, 3, 4].map((n) => ({
    point_id: id(n),
    code: `P${n}`,
    kind: 'INSPECTION' as const,
    x: n,
    y: 0,
    yaw: 0,
    taught_from: 'MANUAL_INPUT' as const,
  })),
  routes: [
    {
      route_id: id(5),
      code: 'R1',
      closed: false,
      points: [
        { point_id: id(1), arrival: 'PASS_THROUGH' },
        { point_id: id(2), arrival: 'STOP' }, // intermediate STOP (e.g. an inspection dwell)
        { point_id: id(3), arrival: 'PASS_THROUGH' },
        { point_id: id(4), arrival: 'STOP' },
      ],
    },
  ],
}

describe('route waypoint arrival is preserved end to end', () => {
  it('load -> save round-trip keeps every arrival, including intermediate STOP', async () => {
    putMock.mockResolvedValue({ data: server })
    const state = toEditorState(server)
    await saveScene(server.scene_id, 3, state)
    const body = putMock.mock.calls[0][1] as { routes: { points: { arrival: string }[] }[] }
    expect(body.routes[0].points.map((p) => p.arrival)).toEqual([
      'PASS_THROUGH',
      'STOP',
      'PASS_THROUGH',
      'STOP',
    ])
  })

  it('reorder / remove / reverse move each arrival with its own waypoint', () => {
    const ed = new SceneEditor(toEditorState(server))
    const rid = id(5)
    const pairs = () =>
      ed.scene.routes[0].point_refs.map((p, i) => `${p.slice(-1)}:${ed.scene.routes[0].arrivals[i]}`)
    expect(pairs()).toEqual(['1:PASS_THROUGH', '2:STOP', '3:PASS_THROUGH', '4:STOP'])
    ed.moveRouteRef(rid, 1, 2) // P2(STOP) after P3
    expect(pairs()).toEqual(['1:PASS_THROUGH', '3:PASS_THROUGH', '2:STOP', '4:STOP'])
    ed.reverseRoute(rid)
    expect(pairs()).toEqual(['4:STOP', '2:STOP', '3:PASS_THROUGH', '1:PASS_THROUGH'])
    ed.removeRouteRef(rid, 1)
    expect(pairs()).toEqual(['4:STOP', '3:PASS_THROUGH', '1:PASS_THROUGH'])
    ed.setArrival(rid, 2, 'STOP')
    ed.setArrival(rid, 0, 'PASS_THROUGH')
    expect(pairs()).toEqual(['4:PASS_THROUGH', '3:PASS_THROUGH', '1:STOP'])
    ed.undo()
    expect(pairs()).toEqual(['4:STOP', '3:PASS_THROUGH', '1:STOP'])
  })
  it('final waypoint must STOP: validation flags a route ending in PASS_THROUGH', () => {
    const ed = new SceneEditor(toEditorState(server))
    ed.setArrival(id(5), 3, 'PASS_THROUGH')
    expect(ed.validate().some((i) => i.code === 'ROUTE_FINAL_WAYPOINT_NOT_STOP')).toBe(true)
  })
})

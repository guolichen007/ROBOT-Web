/**
 * WEB-R1B scene persistence client (storage_version=2, /api/v1/scenes).
 * The server is the authority: it re-validates the import and guards every write with
 * expected_revision (409 REVISION_CONFLICT on a stale editor — never a silent overwrite).
 */
import { api } from '@/lib/api'
import { makePose, type SceneState } from '@/lib/scene-editor'

export interface ServerPoint {
  point_id: string
  code: string
  kind: SceneState['points'][number]['kind']
  x: number
  y: number
  yaw: number
  xy_tolerance_m?: number | null
  yaw_tolerance_rad?: number | null
  taught_from: 'MANUAL_INPUT' | 'MAP_CLICK' | 'VEHICLE_POSE'
}
export interface ServerScene {
  scene_id: string
  map_code: string | null
  version: string
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  storage_version: 2
  revision: number
  traversability: string
  nav_map: {
    resolution_m_per_px: number
    origin: { x: number; y: number; yaw: number | null }
    width_px: number
    height_px: number
    occupied_thresh: number
    free_thresh: number
    negate: 0 | 1
    image_sha256: string
    yaml_sha256: string
    map_checksum: string | null
    image_url: string
  }
  points: ServerPoint[]
  routes: {
    route_id: string
    code: string
    closed: boolean
    points: { point_id: string; arrival: 'PASS_THROUGH' | 'STOP' }[]
  }[]
}

export async function importScene(
  siteId: string,
  mapCode: string,
  version: string,
  yaml: File,
  image: File,
): Promise<ServerScene> {
  const form = new FormData()
  form.append('site_id', siteId)
  form.append('map_code', mapCode)
  form.append('version', version)
  form.append('yaml_file', yaml)
  form.append('image_file', image)
  return (await api.post('/scenes/import', form)).data
}

export async function loadScene(sceneId: string): Promise<ServerScene> {
  return (await api.get(`/scenes/${sceneId}`)).data
}

export async function loadNavImage(scene: ServerScene): Promise<Uint8Array> {
  const res = await api.get(scene.nav_map.image_url.replace(/^\/api\/v1/, ''), {
    responseType: 'arraybuffer',
  })
  return new Uint8Array(res.data as ArrayBuffer)
}

/** Server scene -> editor state (route arrivals: last waypoint STOP, others PASS_THROUGH). */
export function toEditorState(s: ServerScene): SceneState {
  const n = s.nav_map
  return {
    scene_id: s.scene_id,
    scene_version: s.revision,
    base_scene_version: s.revision,
    status: s.status,
    site_code: 'SERVER',
    nav_map: {
      map_code: s.map_code ?? 'UNKNOWN',
      map_version: s.version,
      frame_id: 'map',
      resolution_m_per_px: n.resolution_m_per_px,
      origin: makePose(n.origin.x, n.origin.y, n.origin.yaw ?? 0),
      width_px: n.width_px,
      height_px: n.height_px,
      image_sha256: n.image_sha256,
      yaml_sha256: n.yaml_sha256,
      map_checksum: n.map_checksum ?? '',
      occupied_thresh: n.occupied_thresh,
      free_thresh: n.free_thresh,
      negate: n.negate,
    },
    points: s.points.map((p) => ({
      point_id: p.point_id,
      code: p.code,
      kind: p.kind,
      pose: makePose(p.x, p.y, p.yaw),
      ...(p.xy_tolerance_m ? { xy_tolerance_m: p.xy_tolerance_m } : {}),
      ...(p.yaw_tolerance_rad ? { yaw_tolerance_rad: p.yaw_tolerance_rad } : {}),
      taught_from: p.taught_from,
      parking_slot_code: null,
    })),
    parking_slots: [],
    routes: s.routes.map((r) => ({
      route_id: r.route_id,
      code: r.code,
      closed: r.closed,
      point_refs: r.points.map((x) => x.point_id),
      corridor_refs: [],
    })),
    corridors: [],
  }
}

export async function saveScene(
  sceneId: string,
  expectedRevision: number,
  s: SceneState,
): Promise<ServerScene> {
  return (
    await api.put(`/scenes/${sceneId}`, {
      expected_revision: expectedRevision,
      points: s.points.map((p) => ({
        point_id: p.point_id,
        code: p.code,
        kind: p.kind,
        x: p.pose.x,
        y: p.pose.y,
        yaw: p.pose.yaw,
        xy_tolerance_m: p.xy_tolerance_m ?? null,
        yaw_tolerance_rad: p.yaw_tolerance_rad ?? null,
        taught_from: p.taught_from ?? 'MANUAL_INPUT',
      })),
      routes: s.routes.map((r) => ({
        route_id: r.route_id,
        code: r.code,
        closed: r.closed,
        points: r.point_refs.map((id, i) => ({
          point_id: id,
          arrival: i === r.point_refs.length - 1 ? 'STOP' : 'PASS_THROUGH',
        })),
      })),
    })
  ).data
}

/** Machine code of an API error, e.g. REVISION_CONFLICT; '' when absent. */
export function apiErrorCode(error: unknown): string {
  const e = error as { response?: { data?: { error?: { code?: string } } } }
  return e?.response?.data?.error?.code ?? ''
}

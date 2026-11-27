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
  slot_id?: string | null
}
export interface ServerSlot {
  slot_id: string
  code: string
  polygon: { x: number; y: number }[]
  center_x: number
  center_y: number
  center_yaw: number
  enabled: boolean
}
export interface ServerCorridor {
  corridor_id: string
  code: string
  kind: 'RIGHT_LANE'
  centerline: { x: number; y: number }[]
  width_m: number
}
export interface SceneListItem {
  scene_id: string
  map_code: string
  version: string
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  revision: number
  scene_digest: string | null
  traversability: string
  created_at: string | null
  published_at: string | null
  points: number
  routes: number
}
export interface ServerScene {
  scene_id: string
  map_code: string | null
  version: string
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  storage_version: 2
  revision: number
  traversability: string
  scene_digest?: string | null
  published_at?: string | null
  manifest_url?: string | null
  parking_slots?: ServerSlot[]
  corridors?: ServerCorridor[]
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
    corridor_ids?: string[]
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

/** Server scene -> editor state. Every waypoint keeps its stored arrival (WEB-R1C). */
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
      parking_slot_code: (s.parking_slots ?? []).find((x) => x.slot_id === p.slot_id)?.code ?? null,
    })),
    parking_slots: (s.parking_slots ?? []).map((x) => ({
      slot_id: x.slot_id,
      code: x.code,
      polygon: x.polygon,
      center: makePose(x.center_x, x.center_y, x.center_yaw),
      enabled: x.enabled,
    })),
    routes: s.routes.map((r) => ({
      route_id: r.route_id,
      code: r.code,
      closed: r.closed,
      point_refs: r.points.map((x) => x.point_id),
      arrivals: r.points.map((x) => x.arrival),
      corridor_refs: r.corridor_ids ?? [],
    })),
    corridors: (s.corridors ?? []).map((c) => ({ ...c })),
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
        slot_id: s.parking_slots.find((x) => x.code === p.parking_slot_code)?.slot_id ?? null,
      })),
      parking_slots: s.parking_slots.map((x) => ({
        slot_id: x.slot_id,
        code: x.code,
        polygon: x.polygon,
        center_x: x.center.x,
        center_y: x.center.y,
        center_yaw: x.center.yaw,
        enabled: x.enabled ?? true,
      })),
      corridors: s.corridors.map((c) => ({ ...c })),
      routes: s.routes.map((r) => ({
        route_id: r.route_id,
        code: r.code,
        closed: r.closed,
        points: r.point_refs.map((id, i) => ({
          point_id: id,
          // never re-derived from position: an intermediate STOP stays STOP (WEB-R1C)
          arrival: r.arrivals[i],
        })),
        corridor_ids: r.corridor_refs,
      })),
    })
  ).data
}

export async function listScenes(): Promise<SceneListItem[]> {
  return (await api.get('/scenes')).data
}
export async function setNavMapChecksum(id: string, rev: number, checksum: string): Promise<ServerScene> {
  return (await api.put(`/scenes/${id}/nav-map-checksum`, { expected_revision: rev, map_checksum: checksum }))
    .data
}
export async function publishScene(id: string, rev: number): Promise<ServerScene> {
  return (await api.post(`/scenes/${id}/publish`, { expected_revision: rev })).data
}
/** 当前位置示教: the server reads the vehicle's live pose itself. */
export async function teachPoint(
  id: string,
  rev: number,
  body: { vehicle_id: string; point_id: string; code: string; kind: string },
): Promise<ServerScene> {
  return (await api.post(`/scenes/${id}/teach`, { expected_revision: rev, ...body })).data
}

/** Machine code of an API error, e.g. REVISION_CONFLICT; '' when absent. */
export function apiErrorCode(error: unknown): string {
  const e = error as { response?: { data?: { error?: { code?: string } } } }
  return e?.response?.data?.error?.code ?? ''
}

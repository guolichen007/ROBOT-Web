/**
 * Scene editor model (WEB-V2.1 / WEB-2) — pure, framework-free, unit-testable.
 *
 * Holds ONE DRAFT scene version. Every mutation is world-coordinate based
 * (frame "map", metres, yaw radians CCW from +X). Mutations on a non-DRAFT
 * scene throw SCENE_NOT_DRAFT. Saving/publishing is a server concern; this
 * model only produces a SceneManifest DRAFT payload and local validation hints.
 * Publishing a scene is NOT motion authorisation (R002_OPERATION_CONTRACT §0).
 */
import { newUuid } from '@/lib/id'
import type { NavMapGrid } from '@/lib/nav-map'

export const CONTRACT_VERSION = '2.1.0-draft.2'
export type PointKind = 'INSPECTION' | 'FIRE_OPERATION' | 'RETURN_HOME' | 'WAITING' | 'TRANSIT'
export interface Pose {
  x: number
  y: number
  yaw: number
  frame_id: 'map'
}
export interface ScenePoint {
  point_id: string
  code: string
  kind: PointKind
  pose: Pose
  parking_slot_code?: string | null
  xy_tolerance_m?: number
  yaw_tolerance_rad?: number
  taught_from?: 'MANUAL_INPUT' | 'MAP_CLICK' | 'VEHICLE_POSE'
}
export interface XY {
  x: number
  y: number
}
export interface ParkingSlot {
  slot_id: string
  code: string
  polygon: XY[]
  center: Pose
  enabled?: boolean
}
export interface Route {
  route_id: string
  code: string
  point_refs: string[]
  closed: boolean
  corridor_refs: string[]
}
export interface Corridor {
  corridor_id: string
  code: string
  kind: 'RIGHT_LANE'
  centerline: XY[]
  width_m: number
}
export interface NavMapRef {
  map_code: string
  map_version: string
  frame_id: 'map'
  resolution_m_per_px: number
  origin: Pose
  width_px: number
  height_px: number
  image_sha256: string
  yaml_sha256: string
  map_checksum: string
  occupied_thresh: number
  free_thresh: number
  negate: 0 | 1
}
export interface SceneState {
  scene_id: string
  scene_version: number
  base_scene_version: number | null
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  site_code: string
  nav_map: NavMapRef
  points: ScenePoint[]
  parking_slots: ParkingSlot[]
  routes: Route[]
  corridors: Corridor[]
}
export interface Issue {
  code: string
  ref: string
  level: 'error' | 'warning'
}
/** Live vehicle pose as delivered by VehicleSnapshot.pose (quality-tagged). */
export interface ObservedPose {
  quality: 'LIVE' | 'STALE' | 'UNKNOWN' | 'NOT_CONNECTED' | 'ERROR' | 'UNSUPPORTED'
  value: Pose | null
  localization: string
  map_code: string | null
  map_version: string | null
}

export class SceneEditError extends Error {
  constructor(
    readonly code: string,
    detail = '',
  ) {
    super(detail ? `${code}: ${detail}` : code)
  }
}

export function newSceneId(): string {
  return newUuid()
}

const CODE = /^[A-Za-z0-9_.-]{1,64}$/
const COORD_LIMIT = 100_000

export function normalizeYaw(yaw: number): number {
  if (!Number.isFinite(yaw)) throw new SceneEditError('NON_FINITE_NUMBER', 'yaw')
  let a = yaw % (2 * Math.PI)
  if (a > Math.PI) a -= 2 * Math.PI
  if (a <= -Math.PI) a += 2 * Math.PI
  return a
}

export function makePose(x: number, y: number, yaw: number): Pose {
  for (const [name, v] of [
    ['x', x],
    ['y', y],
  ] as const) {
    if (!Number.isFinite(v)) throw new SceneEditError('NON_FINITE_NUMBER', name)
    if (Math.abs(v) > COORD_LIMIT) throw new SceneEditError('COORDINATE_OUT_OF_RANGE', name)
  }
  return { x, y, yaw: normalizeYaw(yaw), frame_id: 'map' }
}

function checkCode(code: string): void {
  if (!CODE.test(code)) throw new SceneEditError('CODE_INVALID', code)
}

const MAX_HISTORY = 200

export class SceneEditor {
  private state: SceneState
  private past: SceneState[] = []
  private future: SceneState[] = []
  private savedRevision = 0
  private revision = 0

  constructor(
    initial: SceneState,
    private grid?: NavMapGrid,
  ) {
    this.state = structuredClone(initial)
  }

  get scene(): Readonly<SceneState> {
    return this.state
  }
  get canUndo(): boolean {
    return this.past.length > 0
  }
  get canRedo(): boolean {
    return this.future.length > 0
  }
  get dirty(): boolean {
    return this.revision !== this.savedRevision
  }
  markSaved(): void {
    this.savedRevision = this.revision
  }

  setGrid(grid: NavMapGrid | undefined): void {
    this.grid = grid
  }

  private mutate(fn: (draft: SceneState) => void): void {
    if (this.state.status !== 'DRAFT') throw new SceneEditError('SCENE_NOT_DRAFT', this.state.status)
    const next = structuredClone(this.state)
    fn(next) // throws leave state untouched
    this.past.push(this.state)
    if (this.past.length > MAX_HISTORY) this.past.shift()
    this.future = []
    this.state = next
    this.revision++
  }

  undo(): void {
    const prev = this.past.pop()
    if (!prev) return
    this.future.push(this.state)
    this.state = prev
    this.revision--
  }

  redo(): void {
    const next = this.future.pop()
    if (!next) return
    this.past.push(this.state)
    this.state = next
    this.revision++
  }

  // ------------------------------------------------------------------ points
  addPoint(input: {
    kind: PointKind
    code: string
    pose: Pose
    parking_slot_code?: string | null
    taught_from?: ScenePoint['taught_from']
  }): string {
    checkCode(input.code)
    const pose = makePose(input.pose.x, input.pose.y, input.pose.yaw)
    const id = newUuid()
    this.mutate((s) => {
      if (s.points.some((p) => p.code === input.code))
        throw new SceneEditError('DUPLICATE_POINT_CODE', input.code)
      if (input.parking_slot_code && !s.parking_slots.some((x) => x.code === input.parking_slot_code)) {
        throw new SceneEditError('POINT_SLOT_REF_MISSING', input.parking_slot_code)
      }
      s.points.push({
        point_id: id,
        code: input.code,
        kind: input.kind,
        pose,
        parking_slot_code: input.parking_slot_code ?? null,
        taught_from: input.taught_from ?? 'MANUAL_INPUT',
      })
    })
    return id
  }

  /** Drag (x/y), rotate (yaw) or precise input — one undo step each call. */
  updatePoint(
    id: string,
    patch: Partial<
      Pick<ScenePoint, 'code' | 'kind' | 'parking_slot_code' | 'xy_tolerance_m' | 'yaw_tolerance_rad'>
    > & { x?: number; y?: number; yaw?: number },
  ): void {
    if (patch.code !== undefined) checkCode(patch.code)
    this.mutate((s) => {
      const p = s.points.find((q) => q.point_id === id)
      if (!p) throw new SceneEditError('POINT_NOT_FOUND', id)
      if (patch.code && patch.code !== p.code && s.points.some((q) => q.code === patch.code)) {
        throw new SceneEditError('DUPLICATE_POINT_CODE', patch.code)
      }
      p.pose = makePose(patch.x ?? p.pose.x, patch.y ?? p.pose.y, patch.yaw ?? p.pose.yaw)
      if (patch.code !== undefined) p.code = patch.code
      if (patch.kind !== undefined) p.kind = patch.kind
      if (patch.parking_slot_code !== undefined) {
        if (patch.parking_slot_code && !s.parking_slots.some((x) => x.code === patch.parking_slot_code)) {
          throw new SceneEditError('POINT_SLOT_REF_MISSING', patch.parking_slot_code)
        }
        p.parking_slot_code = patch.parking_slot_code
      }
      for (const key of ['xy_tolerance_m', 'yaw_tolerance_rad'] as const) {
        const v = patch[key]
        if (v === undefined) continue
        if (!Number.isFinite(v) || v <= 0) throw new SceneEditError('TOLERANCE_INVALID', key)
        p[key] = v
      }
    })
  }

  deletePoint(id: string): void {
    this.mutate((s) => {
      const used = s.routes.find((r) => r.point_refs.includes(id))
      if (used) throw new SceneEditError('POINT_REFERENCED_BY_ROUTE', used.code)
      const before = s.points.length
      s.points = s.points.filter((p) => p.point_id !== id)
      if (s.points.length === before) throw new SceneEditError('POINT_NOT_FOUND', id)
    })
  }

  /**
   * 当前位置示教: only from a LIVE, localised pose on the SAME nav map.
   * Never from a click converted from pixels.
   */
  teachPoint(kind: PointKind, code: string, observed: ObservedPose): string {
    if (observed.quality !== 'LIVE' || !observed.value)
      throw new SceneEditError('POSE_NOT_LIVE', observed.quality)
    if (observed.localization !== 'OK') throw new SceneEditError('LOCALIZATION_NOT_OK', observed.localization)
    const nav = this.state.nav_map
    if (observed.map_code !== nav.map_code || observed.map_version !== nav.map_version) {
      throw new SceneEditError('POSE_MAP_MISMATCH', `${observed.map_code}/${observed.map_version}`)
    }
    if (observed.value.frame_id !== 'map')
      throw new SceneEditError('POSE_FRAME_INVALID', observed.value.frame_id)
    return this.addPoint({ kind, code, pose: observed.value, taught_from: 'VEHICLE_POSE' })
  }

  // ------------------------------------------------------------------ slots
  addSlot(code: string, polygon: XY[], center: Pose): string {
    checkCode(code)
    if (polygon.length < 3) throw new SceneEditError('POLYGON_TOO_FEW_VERTICES', String(polygon.length))
    const poly = polygon.map((v) => {
      const p = makePose(v.x, v.y, 0)
      return { x: p.x, y: p.y }
    })
    const id = newUuid()
    this.mutate((s) => {
      if (s.parking_slots.some((x) => x.code === code)) throw new SceneEditError('DUPLICATE_SLOT_CODE', code)
      s.parking_slots.push({
        slot_id: id,
        code,
        polygon: poly,
        center: makePose(center.x, center.y, center.yaw),
        enabled: true,
      })
    })
    return id
  }

  deleteSlot(id: string): void {
    this.mutate((s) => {
      const slot = s.parking_slots.find((x) => x.slot_id === id)
      if (!slot) throw new SceneEditError('SLOT_NOT_FOUND', id)
      const user = s.points.find((p) => p.parking_slot_code === slot.code)
      if (user) throw new SceneEditError('SLOT_IN_USE', user.code)
      s.parking_slots = s.parking_slots.filter((x) => x.slot_id !== id)
    })
  }

  // ------------------------------------------------------------------ routes
  addRoute(code: string, pointRefs: string[], closed = false): string {
    checkCode(code)
    const id = newUuid()
    this.mutate((s) => {
      if (s.routes.some((r) => r.code === code)) throw new SceneEditError('DUPLICATE_ROUTE_CODE', code)
      for (const ref of pointRefs) {
        if (!s.points.some((p) => p.point_id === ref))
          throw new SceneEditError('ROUTE_POINT_REF_MISSING', ref)
      }
      s.routes.push({ route_id: id, code, point_refs: [...pointRefs], closed, corridor_refs: [] })
    })
    return id
  }

  appendToRoute(routeId: string, pointId: string): void {
    this.mutate((s) => {
      const r = this.route(s, routeId)
      if (!s.points.some((p) => p.point_id === pointId))
        throw new SceneEditError('ROUTE_POINT_REF_MISSING', pointId)
      if (r.point_refs[r.point_refs.length - 1] === pointId)
        throw new SceneEditError('ROUTE_REPEATED_ADJACENT_POINT', pointId)
      r.point_refs.push(pointId)
    })
  }

  /** Reorder (排序): move the ref at `from` to index `to`. */
  moveRouteRef(routeId: string, from: number, to: number): void {
    this.mutate((s) => {
      const r = this.route(s, routeId)
      if (from < 0 || from >= r.point_refs.length || to < 0 || to >= r.point_refs.length) {
        throw new SceneEditError('ROUTE_INDEX_OUT_OF_RANGE', `${from}->${to}`)
      }
      const [ref] = r.point_refs.splice(from, 1)
      r.point_refs.splice(to, 0, ref)
    })
  }

  removeRouteRef(routeId: string, index: number): void {
    this.mutate((s) => {
      const r = this.route(s, routeId)
      if (index < 0 || index >= r.point_refs.length)
        throw new SceneEditError('ROUTE_INDEX_OUT_OF_RANGE', String(index))
      r.point_refs.splice(index, 1)
    })
  }

  reverseRoute(routeId: string): void {
    this.mutate((s) => {
      this.route(s, routeId).point_refs.reverse()
    })
  }

  linkCorridor(routeId: string, corridorId: string): void {
    this.mutate((s) => {
      const r = this.route(s, routeId)
      if (!s.corridors.some((c) => c.corridor_id === corridorId))
        throw new SceneEditError('ROUTE_CORRIDOR_REF_MISSING', corridorId)
      if (!r.corridor_refs.includes(corridorId)) r.corridor_refs.push(corridorId)
    })
  }

  private route(s: SceneState, id: string): Route {
    const r = s.routes.find((x) => x.route_id === id)
    if (!r) throw new SceneEditError('ROUTE_NOT_FOUND', id)
    return r
  }

  // ------------------------------------------------------------------ corridors (右车道)
  addCorridor(code: string, centerline: XY[], widthM: number): string {
    checkCode(code)
    if (centerline.length < 2) throw new SceneEditError('CORRIDOR_TOO_SHORT')
    if (!(widthM >= 0.5 && widthM <= 10))
      throw new SceneEditError('CORRIDOR_WIDTH_OUT_OF_RANGE', String(widthM))
    const line = centerline.map((v) => {
      const p = makePose(v.x, v.y, 0)
      return { x: p.x, y: p.y }
    })
    const id = newUuid()
    this.mutate((s) => {
      if (s.corridors.some((c) => c.code === code)) throw new SceneEditError('DUPLICATE_CORRIDOR_CODE', code)
      s.corridors.push({ corridor_id: id, code, kind: 'RIGHT_LANE', centerline: line, width_m: widthM })
    })
    return id
  }

  // ------------------------------------------------------------------ preview & validation
  /** Direction preview: ordered segments with heading (rad) for arrow rendering. */
  routeSegments(routeId: string): { from: XY; to: XY; heading: number }[] {
    const r = this.route(this.state, routeId)
    const pts = r.point_refs
      .map((ref) => this.state.points.find((p) => p.point_id === ref)?.pose)
      .filter((p): p is Pose => !!p)
    const seq = r.closed && pts.length > 2 ? [...pts, pts[0]] : pts
    return seq.slice(1).map((to, i) => ({
      from: { x: seq[i].x, y: seq[i].y },
      to: { x: to.x, y: to.y },
      heading: Math.atan2(to.y - seq[i].y, to.x - seq[i].x),
    }))
  }

  /** Right-lane polygon (lane sits on the right of travel direction). */
  corridorPolygon(corridorId: string): XY[] {
    const c = this.state.corridors.find((x) => x.corridor_id === corridorId)
    if (!c) throw new SceneEditError('CORRIDOR_NOT_FOUND', corridorId)
    const left: XY[] = []
    const right: XY[] = []
    c.centerline.forEach((p, i) => {
      const a = c.centerline[Math.max(0, i - 1)]
      const b = c.centerline[Math.min(c.centerline.length - 1, i + 1)]
      const len = Math.hypot(b.x - a.x, b.y - a.y) || 1
      // right normal of travel direction = (dy, -dx)
      const nx = (b.y - a.y) / len
      const ny = -(b.x - a.x) / len
      left.push({ x: p.x, y: p.y })
      right.push({ x: p.x + nx * c.width_m, y: p.y + ny * c.width_m })
    })
    return [...left, ...right.reverse()]
  }

  /**
   * Local pre-publish hints (server is authoritative). Traversability is never
   * claimed here: it stays PENDING_VERIFICATION until a real planner check.
   */
  validate(): Issue[] {
    const s = this.state
    const issues: Issue[] = []
    const ids = new Set(s.points.map((p) => p.point_id))
    const slotCodes = new Set(s.parking_slots.map((x) => x.code))
    const corridorIds = new Set(s.corridors.map((c) => c.corridor_id))
    for (const p of s.points) {
      if (p.parking_slot_code && !slotCodes.has(p.parking_slot_code))
        issues.push({ code: 'POINT_SLOT_REF_MISSING', ref: p.code, level: 'error' })
      if (this.grid && !this.grid.contains(p.pose))
        issues.push({ code: 'POINT_OUTSIDE_NAV_MAP', ref: p.code, level: 'error' })
    }
    for (const r of s.routes) {
      if (r.point_refs.length < 2) issues.push({ code: 'ROUTE_TOO_SHORT', ref: r.code, level: 'error' })
      for (const ref of r.point_refs)
        if (!ids.has(ref)) issues.push({ code: 'ROUTE_POINT_REF_MISSING', ref: r.code, level: 'error' })
      r.point_refs.forEach((ref, i) => {
        if (i > 0 && r.point_refs[i - 1] === ref)
          issues.push({ code: 'ROUTE_REPEATED_ADJACENT_POINT', ref: r.code, level: 'error' })
      })
      for (const ref of r.corridor_refs)
        if (!corridorIds.has(ref))
          issues.push({ code: 'ROUTE_CORRIDOR_REF_MISSING', ref: r.code, level: 'error' })
    }
    if (!s.points.some((p) => p.kind === 'RETURN_HOME'))
      issues.push({ code: 'RETURN_HOME_POINT_MISSING', ref: 'points', level: 'error' })
    if (!s.nav_map.map_checksum)
      issues.push({ code: 'NAV_MAP_IDENTITY_INCOMPLETE', ref: 'nav_map', level: 'error' })
    issues.push({ code: 'TRAVERSABILITY_PENDING_VERIFICATION', ref: 'validation', level: 'warning' })
    return issues
  }

  /** SceneManifest DRAFT payload (contract 2.1.0-draft.2); digest stays null until server publish. */
  toManifest(createdAt = new Date().toISOString()): Record<string, unknown> {
    const issues = this.validate()
    const errors = issues.filter((i) => i.level === 'error')
    return {
      contract_version: CONTRACT_VERSION,
      kind: 'SceneManifest',
      ...structuredClone(this.state),
      validation: {
        schema_ok: true,
        references_ok: errors.length === 0,
        traversability: 'PENDING_VERIFICATION',
        traversability_evidence: null,
        checked_at: createdAt,
        issues: errors.map(({ code, ref }) => ({ code, ref })),
      },
      digest: null,
      created_at: createdAt,
      published_at: null,
      published_by: null,
    }
  }
}

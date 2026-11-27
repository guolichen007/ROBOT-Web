/**
 * ROS map_server navigation map import (PGM/PNG + YAML) — WEB-V2.1 / WEB-2.
 *
 * Browser-side pre-check and preview only. The API MUST repeat every check
 * (R002_SCENE_CONTRACT §4). Nothing here builds a navigation goal: goals are
 * always world coordinates in frame "map" (地图坐标合同), never browser pixels.
 *
 * ROS convention: `origin` is the pose of the LOWER-LEFT corner of the image in
 * frame map; image row 0 is the TOP row; resolution is metres per pixel.
 */
import type { MapGeometry, Point } from '@/lib/map-adapter'

export const NAV_MAP_LIMITS = {
  yamlMaxBytes: 64 * 1024,
  imageMaxBytes: 20 * 1024 * 1024,
  maxSidePx: 8192,
  maxPixels: 32_000_000,
  maxResolution: 1,
  maxAbsOrigin: 100_000,
} as const

export class NavMapImportError extends Error {
  constructor(
    readonly code: string,
    detail = '',
  ) {
    super(detail ? `${code}: ${detail}` : code)
  }
}

export interface NavMapMeta {
  image: string
  resolution: number
  origin: { x: number; y: number; yaw: number }
  negate: 0 | 1
  occupied_thresh: number
  free_thresh: number
  mode: 'trinary' | 'scale' | 'raw'
}

const ALLOWED_KEYS = new Set([
  'image',
  'resolution',
  'origin',
  'negate',
  'occupied_thresh',
  'free_thresh',
  'mode',
])
const IMAGE_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\.(pgm|png)$/i
const NUMBER = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/

function finite(raw: string, key: string): number {
  if (!NUMBER.test(raw)) throw new NavMapImportError('YAML_NOT_A_FINITE_NUMBER', key)
  const value = Number(raw)
  if (!Number.isFinite(value)) throw new NavMapImportError('YAML_NOT_A_FINITE_NUMBER', key)
  return value
}

function unquote(raw: string): string {
  const m = /^(['"])(.*)\1$/.exec(raw)
  return m ? m[2] : raw
}

/**
 * Strict whitelist parser for the flat map_server YAML subset.
 * Rejects anchors/aliases/tags/multi-docs/nesting/duplicate or unknown keys.
 */
export function parseMapYaml(text: string, byteLength = new TextEncoder().encode(text).length): NavMapMeta {
  if (byteLength > NAV_MAP_LIMITS.yamlMaxBytes)
    throw new NavMapImportError('YAML_TOO_LARGE', String(byteLength))
  const seen = new Map<string, string>()
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/)
  for (const [index, line] of lines.entries()) {
    const stripped = line
      .replace(/\s+#.*$/, '')
      .replace(/^#.*$/, '')
      .trimEnd()
    if (!stripped.trim()) continue
    if (stripped.trim() === '---' && index === 0) continue
    if (/^\s/.test(stripped)) throw new NavMapImportError('YAML_NESTING_FORBIDDEN', `line ${index + 1}`)
    if (/[&*!|>{}]/.test(stripped) || stripped.startsWith('---') || stripped.startsWith('...')) {
      throw new NavMapImportError('YAML_FEATURE_FORBIDDEN', `line ${index + 1}`)
    }
    const m = /^([A-Za-z_]+):\s*(.*)$/.exec(stripped)
    if (!m) throw new NavMapImportError('YAML_SYNTAX', `line ${index + 1}`)
    const [, key, value] = m
    if (!ALLOWED_KEYS.has(key)) throw new NavMapImportError('YAML_UNKNOWN_KEY', key)
    if (seen.has(key)) throw new NavMapImportError('YAML_DUPLICATE_KEY', key)
    seen.set(key, value.trim())
  }
  for (const key of ['image', 'resolution', 'origin', 'occupied_thresh', 'free_thresh']) {
    if (!seen.has(key)) throw new NavMapImportError('YAML_MISSING_KEY', key)
  }
  const image = unquote(seen.get('image')!)
  if (!IMAGE_NAME.test(image) || image.includes('..')) {
    throw new NavMapImportError('YAML_IMAGE_PATH_FORBIDDEN', image)
  }
  const resolution = finite(seen.get('resolution')!, 'resolution')
  if (!(resolution > 0 && resolution <= NAV_MAP_LIMITS.maxResolution)) {
    throw new NavMapImportError('YAML_RESOLUTION_OUT_OF_RANGE', String(resolution))
  }
  const originMatch = /^\[\s*([^,\]]+)\s*,\s*([^,\]]+)\s*,\s*([^,\]]+)\s*\]$/.exec(seen.get('origin')!)
  if (!originMatch) throw new NavMapImportError('YAML_ORIGIN_SHAPE', seen.get('origin'))
  const [ox, oy, oyaw] = originMatch.slice(1, 4).map((v, i) => finite(v.trim(), `origin[${i}]`))
  if (Math.abs(ox) > NAV_MAP_LIMITS.maxAbsOrigin || Math.abs(oy) > NAV_MAP_LIMITS.maxAbsOrigin) {
    throw new NavMapImportError('YAML_ORIGIN_OUT_OF_RANGE')
  }
  if (Math.abs(oyaw) > Math.PI + 1e-9)
    throw new NavMapImportError('YAML_ORIGIN_YAW_OUT_OF_RANGE', String(oyaw))
  const occupied = finite(seen.get('occupied_thresh')!, 'occupied_thresh')
  const free = finite(seen.get('free_thresh')!, 'free_thresh')
  if (!(occupied >= 0 && occupied <= 1 && free >= 0 && free <= 1 && free < occupied)) {
    throw new NavMapImportError('YAML_THRESHOLDS_INVALID', `free=${free} occupied=${occupied}`)
  }
  const negateRaw = seen.get('negate') ?? '0'
  if (negateRaw !== '0' && negateRaw !== '1') throw new NavMapImportError('YAML_NEGATE_INVALID', negateRaw)
  const mode = unquote(seen.get('mode') ?? 'trinary')
  if (mode !== 'trinary' && mode !== 'scale' && mode !== 'raw')
    throw new NavMapImportError('YAML_MODE_INVALID', mode)
  return {
    image,
    resolution,
    origin: { x: ox, y: oy, yaw: oyaw },
    negate: negateRaw === '1' ? 1 : 0,
    occupied_thresh: occupied,
    free_thresh: free,
    mode,
  }
}

export interface RasterInfo {
  format: 'pgm' | 'png'
  width: number
  height: number
  /** 8-bit grey values row-major (top row first); only for PGM. */
  data?: Uint8Array
  maxval?: number
}

function checkDims(width: number, height: number): void {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1) {
    throw new NavMapImportError('IMAGE_DIMENSIONS_INVALID', `${width}x${height}`)
  }
  if (
    width > NAV_MAP_LIMITS.maxSidePx ||
    height > NAV_MAP_LIMITS.maxSidePx ||
    width * height > NAV_MAP_LIMITS.maxPixels
  ) {
    throw new NavMapImportError('IMAGE_TOO_MANY_PIXELS', `${width}x${height}`)
  }
}

/** Binary (P5) or ASCII (P2) PGM, maxval <= 255. */
export function parsePgm(bytes: Uint8Array): RasterInfo {
  if (bytes.length > NAV_MAP_LIMITS.imageMaxBytes)
    throw new NavMapImportError('IMAGE_TOO_LARGE', String(bytes.length))
  let pos = 0
  const token = (): string => {
    for (;;) {
      while (pos < bytes.length && /\s/.test(String.fromCharCode(bytes[pos]))) pos++
      if (bytes[pos] === 0x23) {
        while (pos < bytes.length && bytes[pos] !== 0x0a) pos++
        continue
      }
      break
    }
    const start = pos
    while (pos < bytes.length && !/\s/.test(String.fromCharCode(bytes[pos]))) pos++
    if (start === pos) throw new NavMapImportError('PGM_HEADER_TRUNCATED')
    return new TextDecoder('ascii').decode(bytes.subarray(start, pos))
  }
  const magic = token()
  if (magic !== 'P5' && magic !== 'P2') throw new NavMapImportError('PGM_MAGIC_UNSUPPORTED', magic)
  const int = (name: string): number => {
    const t = token()
    if (!/^\d{1,6}$/.test(t)) throw new NavMapImportError('PGM_HEADER_INVALID', name)
    return Number(t)
  }
  const width = int('width')
  const height = int('height')
  const maxval = int('maxval')
  checkDims(width, height)
  if (maxval < 1 || maxval > 255) throw new NavMapImportError('PGM_MAXVAL_UNSUPPORTED', String(maxval))
  const count = width * height
  let data: Uint8Array
  if (magic === 'P5') {
    pos += 1 // single whitespace after maxval
    if (bytes.length - pos < count)
      throw new NavMapImportError('PGM_DATA_TRUNCATED', `${bytes.length - pos}<${count}`)
    data = bytes.slice(pos, pos + count)
  } else {
    data = new Uint8Array(count)
    for (let i = 0; i < count; i++) {
      const v = int('pixel')
      if (v > maxval) throw new NavMapImportError('PGM_PIXEL_OVER_MAXVAL', String(i))
      data[i] = v
    }
  }
  return { format: 'pgm', width, height, maxval, data }
}

const PNG_SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

/** PNG dimensions from IHDR only (decode is left to the browser for preview). */
export function parsePngHeader(bytes: Uint8Array): RasterInfo {
  if (bytes.length > NAV_MAP_LIMITS.imageMaxBytes)
    throw new NavMapImportError('IMAGE_TOO_LARGE', String(bytes.length))
  if (bytes.length < 24 || PNG_SIG.some((b, i) => bytes[i] !== b))
    throw new NavMapImportError('PNG_SIGNATURE_INVALID')
  const chunk = new TextDecoder('ascii').decode(bytes.subarray(12, 16))
  if (chunk !== 'IHDR') throw new NavMapImportError('PNG_IHDR_MISSING')
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const width = view.getUint32(16)
  const height = view.getUint32(20)
  checkDims(width, height)
  return { format: 'png', width, height }
}

export function parseRaster(fileName: string, bytes: Uint8Array): RasterInfo {
  if (/\.pgm$/i.test(fileName)) return parsePgm(bytes)
  if (/\.png$/i.test(fileName)) return parsePngHeader(bytes)
  throw new NavMapImportError('IMAGE_FORMAT_UNSUPPORTED', fileName)
}

/** Pairs the YAML with exactly the uploaded image it names (no other path is ever resolved). */
export function bindImage(meta: NavMapMeta, uploadedName: string): void {
  const base = uploadedName.split(/[\\/]/).pop() ?? ''
  if (base !== meta.image) throw new NavMapImportError('YAML_IMAGE_NAME_MISMATCH', `${meta.image} != ${base}`)
}

/** Exact ROS pixel <-> world transform. Pixel (col,row): row 0 = top; centre at +0.5. */
export class NavMapGrid {
  readonly widthM: number
  readonly heightM: number
  private readonly cos: number
  private readonly sin: number

  constructor(
    readonly meta: NavMapMeta,
    readonly widthPx: number,
    readonly heightPx: number,
  ) {
    checkDims(widthPx, heightPx)
    this.widthM = widthPx * meta.resolution
    this.heightM = heightPx * meta.resolution
    this.cos = Math.cos(meta.origin.yaw)
    this.sin = Math.sin(meta.origin.yaw)
  }

  pixelToWorld(col: number, row: number): Point {
    const u = col * this.meta.resolution
    const v = (this.heightPx - row) * this.meta.resolution
    return {
      x: this.meta.origin.x + u * this.cos - v * this.sin,
      y: this.meta.origin.y + u * this.sin + v * this.cos,
    }
  }

  pixelCenterToWorld(col: number, row: number): Point {
    return this.pixelToWorld(col + 0.5, row + 0.5)
  }

  worldToPixel(p: Point): { col: number; row: number } {
    const dx = p.x - this.meta.origin.x
    const dy = p.y - this.meta.origin.y
    const u = dx * this.cos + dy * this.sin
    const v = -dx * this.sin + dy * this.cos
    return { col: u / this.meta.resolution, row: this.heightPx - v / this.meta.resolution }
  }

  contains(p: Point): boolean {
    const { col, row } = this.worldToPixel(p)
    return col >= 0 && row >= 0 && col < this.widthPx && row < this.heightPx
  }

  /** Geometry for MapAdapter (it applies R(rotation)·(p-origin), so rotation = -yaw). */
  geometry(): MapGeometry {
    return {
      width_m: this.widthM,
      height_m: this.heightM,
      origin_x: this.meta.origin.x,
      origin_y: this.meta.origin.y,
      rotation_rad: -this.meta.origin.yaw,
    }
  }

  /** Occupancy per map_server trinary rule; null = unknown. Requires PGM data. */
  occupancy(raster: RasterInfo, col: number, row: number): 'free' | 'occupied' | null {
    if (!raster.data || !raster.maxval) return null
    const value = raster.data[row * this.widthPx + col]
    const p = this.meta.negate ? value / raster.maxval : (raster.maxval - value) / raster.maxval
    if (p > this.meta.occupied_thresh) return 'occupied'
    if (p < this.meta.free_thresh) return 'free'
    return null
  }
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes))
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
}

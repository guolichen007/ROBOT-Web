import { describe, expect, it } from 'vitest'
import { MapAdapter } from '@/lib/map-adapter'
import {
  NavMapGrid,
  NavMapImportError,
  bindImage,
  parseMapYaml,
  parsePgm,
  parsePngHeader,
} from '@/lib/nav-map'

const YAML = `image: sim_r002.pgm
resolution: 0.05
origin: [-10.0, -5.0, 0.0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
`

function code(fn: () => unknown): string {
  try {
    fn()
  } catch (e) {
    return (e as NavMapImportError).code
  }
  return 'NO_ERROR'
}

function pgm(w: number, h: number, fill = 254): Uint8Array {
  const header = new TextEncoder().encode(`P5\n# comment\n${w} ${h}\n255\n`)
  const out = new Uint8Array(header.length + w * h)
  out.set(header)
  out.fill(fill, header.length)
  return out
}

describe('parseMapYaml (strict whitelist)', () => {
  it('parses a map_server yaml', () => {
    const m = parseMapYaml(YAML)
    expect(m).toMatchObject({ image: 'sim_r002.pgm', resolution: 0.05, negate: 0, mode: 'trinary' })
    expect(m.origin).toEqual({ x: -10, y: -5, yaw: 0 })
  })
  it.each([
    ['absolute image path', YAML.replace('sim_r002.pgm', '/etc/passwd.pgm'), 'YAML_IMAGE_PATH_FORBIDDEN'],
    ['traversal', YAML.replace('sim_r002.pgm', '../x.pgm'), 'YAML_IMAGE_PATH_FORBIDDEN'],
    ['url', YAML.replace('sim_r002.pgm', 'http://h/x.png'), 'YAML_IMAGE_PATH_FORBIDDEN'],
    ['nan', YAML.replace('0.05', '.nan'), 'YAML_NOT_A_FINITE_NUMBER'],
    ['inf origin', YAML.replace('-10.0', '.inf'), 'YAML_NOT_A_FINITE_NUMBER'],
    ['zero resolution', YAML.replace('0.05', '0'), 'YAML_RESOLUTION_OUT_OF_RANGE'],
    ['inverted thresholds', YAML.replace('0.196', '0.9'), 'YAML_THRESHOLDS_INVALID'],
    ['duplicate key', YAML + 'resolution: 0.1\n', 'YAML_DUPLICATE_KEY'],
    ['unknown key', YAML + 'evil: 1\n', 'YAML_UNKNOWN_KEY'],
    ['alias bomb', YAML + 'mode: *a\n', 'YAML_FEATURE_FORBIDDEN'],
    ['nested', YAML + '  nested: 1\n', 'YAML_NESTING_FORBIDDEN'],
    ['missing origin', YAML.replace(/origin.*\n/, ''), 'YAML_MISSING_KEY'],
    ['bad negate', YAML.replace('negate: 0', 'negate: 2'), 'YAML_NEGATE_INVALID'],
    ['origin yaw > pi', YAML.replace('0.0]', '4.0]'), 'YAML_ORIGIN_YAW_OUT_OF_RANGE'],
  ])('rejects %s', (_, text, expected) => {
    expect(code(() => parseMapYaml(text))).toBe(expected)
  })
  it('rejects oversize yaml before parsing', () => {
    expect(code(() => parseMapYaml(YAML, 70 * 1024))).toBe('YAML_TOO_LARGE')
  })
  it('binds only the exactly named uploaded image', () => {
    const m = parseMapYaml(YAML)
    expect(code(() => bindImage(m, 'C:\\maps\\sim_r002.pgm'))).toBe('NO_ERROR')
    expect(code(() => bindImage(m, 'other.pgm'))).toBe('YAML_IMAGE_NAME_MISMATCH')
  })
})

describe('raster headers', () => {
  it('parses binary PGM with comments', () => {
    const r = parsePgm(pgm(4, 3))
    expect([r.width, r.height, r.maxval, r.data?.length]).toEqual([4, 3, 255, 12])
  })
  it('rejects truncated PGM data and oversize dimensions', () => {
    expect(code(() => parsePgm(pgm(4, 3).slice(0, -1)))).toBe('PGM_DATA_TRUNCATED')
    const huge = new TextEncoder().encode('P5 9000 10 255\n')
    expect(code(() => parsePgm(huge))).toBe('IMAGE_TOO_MANY_PIXELS')
    expect(code(() => parsePgm(new TextEncoder().encode('P6 1 1 255\n')))).toBe('PGM_MAGIC_UNSUPPORTED')
  })
  it('reads PNG IHDR and rejects bad signature', () => {
    const png = new Uint8Array(24)
    png.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52])
    new DataView(png.buffer).setUint32(16, 400)
    new DataView(png.buffer).setUint32(20, 300)
    expect(parsePngHeader(png)).toMatchObject({ width: 400, height: 300 })
    png[1] = 0
    expect(code(() => parsePngHeader(png))).toBe('PNG_SIGNATURE_INVALID')
  })
})

describe('NavMapGrid world <-> pixel (ROS convention)', () => {
  const meta = parseMapYaml(YAML)
  const grid = new NavMapGrid(meta, 400, 200)

  it('lower-left pixel corner is the YAML origin; row 0 is the top', () => {
    expect(grid.pixelToWorld(0, 200)).toEqual({ x: -10, y: -5 })
    const top = grid.pixelToWorld(0, 0)
    expect(top.x).toBeCloseTo(-10, 9)
    expect(top.y).toBeCloseTo(5, 9)
    const c = grid.pixelCenterToWorld(0, 199)
    expect(c.x).toBeCloseTo(-9.975, 9)
    expect(c.y).toBeCloseTo(-4.975, 9)
  })
  it('round-trips with rotated origin', () => {
    const rotated = new NavMapGrid({ ...meta, origin: { x: 3, y: -2, yaw: 0.7 } }, 400, 200)
    for (const [col, row] of [
      [0, 0],
      [123.5, 77.25],
      [399, 199],
    ]) {
      const back = rotated.worldToPixel(rotated.pixelToWorld(col, row))
      expect(back.col).toBeCloseTo(col, 9)
      expect(back.row).toBeCloseTo(row, 9)
    }
  })
  it('contains() bounds the map', () => {
    expect(grid.contains({ x: 0, y: 0 })).toBe(true)
    expect(grid.contains({ x: 10.01, y: 0 })).toBe(false)
  })
  it('feeds MapAdapter so screen round-trips stay in world metres', () => {
    const rotated = new NavMapGrid({ ...meta, origin: { x: 3, y: -2, yaw: 0.7 } }, 400, 200)
    const adapter = new MapAdapter(rotated.geometry(), { width: 1000, height: 700 })
    const w = rotated.pixelCenterToWorld(200, 100)
    const back = adapter.screenToWorld(adapter.worldToScreen(w))
    expect(back.x).toBeCloseTo(w.x, 9)
    expect(back.y).toBeCloseTo(w.y, 9)
  })
  it('classifies occupancy with the trinary rule', () => {
    const raster = parsePgm(pgm(400, 200, 254))
    raster.data![0] = 0
    raster.data![1] = 205
    expect(grid.occupancy(raster, 2, 0)).toBe('free')
    expect(grid.occupancy(raster, 0, 0)).toBe('occupied')
    expect(grid.occupancy(raster, 1, 0)).toBeNull()
  })
})

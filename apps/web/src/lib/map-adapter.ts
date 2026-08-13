export interface Point {
  x: number
  y: number
}
export interface Viewport {
  width: number
  height: number
}
export interface MapGeometry {
  width_m: number
  height_m: number
  origin_x: number
  origin_y: number
  rotation_rad: number
}

export class MapAdapter {
  private zoom = 1
  private pan: Point = { x: 0, y: 0 }
  private readonly padding = 28

  constructor(
    private map: MapGeometry,
    private viewport: Viewport,
  ) {}

  setViewport(viewport: Viewport): void {
    this.viewport = viewport
  }
  setMap(map: MapGeometry): void {
    this.map = map
  }
  setZoom(zoom: number): void {
    this.zoom = Math.min(4, Math.max(0.6, zoom))
  }
  getZoom(): number {
    return this.zoom
  }
  getPan(): Point {
    return { ...this.pan }
  }
  centerOn(point: Point): void {
    const screen = this.worldToScreen(point)
    this.panBy(this.viewport.width / 2 - screen.x, this.viewport.height / 2 - screen.y)
  }
  panBy(dx: number, dy: number): void {
    this.pan = { x: this.pan.x + dx, y: this.pan.y + dy }
  }
  reset(): void {
    this.zoom = 1
    this.pan = { x: 0, y: 0 }
  }

  private baseScale(): number {
    return Math.min(
      (this.viewport.width - this.padding * 2) / this.map.width_m,
      (this.viewport.height - this.padding * 2) / this.map.height_m,
    )
  }

  worldToScreen(point: Point): Point {
    const localX = point.x - this.map.origin_x
    const localY = point.y - this.map.origin_y
    const cos = Math.cos(this.map.rotation_rad)
    const sin = Math.sin(this.map.rotation_rad)
    const rotatedX = localX * cos - localY * sin
    const rotatedY = localX * sin + localY * cos
    const scale = this.baseScale() * this.zoom
    const contentWidth = this.map.width_m * scale
    const contentHeight = this.map.height_m * scale
    return {
      x: (this.viewport.width - contentWidth) / 2 + rotatedX * scale + this.pan.x,
      y: (this.viewport.height + contentHeight) / 2 - rotatedY * scale + this.pan.y,
    }
  }

  screenToWorld(point: Point): Point {
    const scale = this.baseScale() * this.zoom
    const contentWidth = this.map.width_m * scale
    const contentHeight = this.map.height_m * scale
    const rotatedX = (point.x - (this.viewport.width - contentWidth) / 2 - this.pan.x) / scale
    const rotatedY = ((this.viewport.height + contentHeight) / 2 + this.pan.y - point.y) / scale
    const cos = Math.cos(-this.map.rotation_rad)
    const sin = Math.sin(-this.map.rotation_rad)
    return {
      x: rotatedX * cos - rotatedY * sin + this.map.origin_x,
      y: rotatedX * sin + rotatedY * cos + this.map.origin_y,
    }
  }
}

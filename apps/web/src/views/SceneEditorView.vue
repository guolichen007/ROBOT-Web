<template>
  <section class="scene-editor" data-testid="scene-editor">
    <header class="se-bar">
      <div class="se-title">
        <h1>地图场景编辑器</h1>
        <span class="se-status" :data-status="editor?.scene.status ?? 'NONE'">{{ statusText }}</span>
        <span v-if="editor" class="se-note"
          >本地草稿 · 未保存到服务器 · 发布与车端加载需服务端（待审查）· 发布≠运动授权</span
        >
      </div>
      <div class="se-actions">
        <label class="se-file">
          导入 YAML + PGM/PNG
          <input
            data-testid="se-import"
            type="file"
            multiple
            accept=".yaml,.yml,.pgm,.png"
            @change="onImport"
          />
        </label>
        <button
          type="button"
          :disabled="!editor?.canUndo"
          data-testid="se-undo"
          @click="act(() => editor!.undo())"
        >
          撤销
        </button>
        <button
          type="button"
          :disabled="!editor?.canRedo"
          data-testid="se-redo"
          @click="act(() => editor!.redo())"
        >
          重做
        </button>
        <button type="button" :disabled="!editor" data-testid="se-validate" @click="runValidate">
          发布前校验
        </button>
      </div>
    </header>

    <p v-if="error" class="se-error" role="alert" data-testid="se-error">{{ error }}</p>

    <div v-if="!editor" class="se-empty">
      <p>
        导入 ROS map_server 导出的 YAML 与同名 PGM/PNG
        后开始编辑。文件只在浏览器内解析预览，服务端将重复校验。
      </p>
      <p>限制：YAML ≤ 64 KiB，图像 ≤ 20 MiB，单边 ≤ 8192 px；YAML 只允许引用同时上传的同名图像。</p>
    </div>

    <div v-else class="se-body">
      <aside class="se-tree" aria-label="资产树">
        <div v-for="group in groups" :key="group.kind" class="se-group">
          <div class="se-group-head">
            <span>{{ group.label }}（{{ group.items.length }}）</span>
            <button
              type="button"
              :class="{ active: addKind === group.kind }"
              :data-testid="`se-add-${group.kind}`"
              @click="addKind = addKind === group.kind ? null : group.kind"
            >
              {{ addKind === group.kind ? '点击地图放置…' : '新增' }}
            </button>
          </div>
          <button
            v-for="p in group.items"
            :key="p.point_id"
            type="button"
            class="se-item"
            :class="{ selected: p.point_id === selectedId }"
            @click="selectedId = p.point_id"
          >
            {{ p.code }} <span class="num">{{ p.pose.x.toFixed(2) }}, {{ p.pose.y.toFixed(2) }}</span>
          </button>
        </div>
        <div class="se-group">
          <div class="se-group-head">
            <span>路线（{{ editor.scene.routes.length }}）</span>
            <button
              type="button"
              :disabled="routeCandidates.length < 2"
              data-testid="se-add-route"
              @click="addRoute"
            >
              由巡检点生成
            </button>
          </div>
          <div v-for="r in editor.scene.routes" :key="r.route_id" class="se-item static">
            {{ r.code }} · {{ r.point_refs.length }} 点
            <button type="button" class="link" @click="act(() => editor!.reverseRoute(r.route_id))">
              反向
            </button>
          </div>
        </div>
      </aside>

      <div ref="canvasHost" class="se-canvas" data-testid="se-canvas">
        <svg
          :width="vp.width"
          :height="vp.height"
          @click="onCanvasClick"
          @pointermove="onMove"
          @pointerup="endDrag"
          @pointerleave="endDrag"
        >
          <image
            v-if="preview"
            :href="preview"
            :x="imgBox.x"
            :y="imgBox.y"
            :width="imgBox.w"
            :height="imgBox.h"
            :transform="imgBox.t"
            preserveAspectRatio="none"
          />
          <g v-for="r in editor.scene.routes" :key="r.route_id">
            <line
              v-for="(s, i) in editor.routeSegments(r.route_id)"
              :key="i"
              v-bind="seg(s)"
              class="se-route"
              marker-end="url(#se-arrow)"
            />
          </g>
          <defs>
            <marker
              id="se-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M0,0 L10,5 L0,10 z" class="se-arrow" />
            </marker>
          </defs>
          <g
            v-for="p in editor.scene.points"
            :key="p.point_id"
            :transform="`translate(${scr(posOf(p)).x},${scr(posOf(p)).y})`"
            class="se-point"
            :class="[p.kind, { selected: p.point_id === selectedId }]"
            :data-testid="`se-point-${p.code}`"
            @pointerdown.stop="startDrag(p.point_id)"
            @click.stop="selectedId = p.point_id"
          >
            <g :transform="`rotate(${headingDeg(p)})`">
              <circle r="8" />
              <line x1="0" y1="0" x2="16" y2="0" />
            </g>
            <text x="11" y="-10">{{ p.code }}</text>
          </g>
        </svg>
        <div class="se-cursor num" aria-live="polite">{{ cursorText }}</div>
      </div>

      <aside class="se-inspector" aria-label="属性检查器" data-testid="se-inspector">
        <template v-if="selected">
          <h2>{{ kindLabel[selected.kind] }}</h2>
          <label
            >编号<input
              :value="selected.code"
              data-testid="se-code"
              @change="patch({ code: ($event.target as HTMLInputElement).value })"
          /></label>
          <label
            >X（米）<input
              type="number"
              step="0.01"
              :value="selected.pose.x"
              data-testid="se-x"
              @change="patch({ x: num($event) })"
          /></label>
          <label
            >Y（米）<input
              type="number"
              step="0.01"
              :value="selected.pose.y"
              data-testid="se-y"
              @change="patch({ y: num($event) })"
          /></label>
          <label
            >yaw（度，0=+X 逆时针）<input
              type="number"
              step="1"
              :value="((selected.pose.yaw * 180) / Math.PI).toFixed(1)"
              data-testid="se-yaw"
              @change="patch({ yaw: (num($event) * Math.PI) / 180 })"
          /></label>
          <p class="se-meta">来源：{{ taughtLabel[selected.taught_from ?? 'MANUAL_INPUT'] }} · frame=map</p>
          <button
            type="button"
            class="danger"
            data-testid="se-delete"
            @click="
              act(() => {
                editor!.deletePoint(selected!.point_id)
                selectedId = null
              })
            "
          >
            删除点位
          </button>
        </template>
        <template v-else>
          <h2>导航地图</h2>
          <dl class="se-meta-list num">
            <dt>图像</dt>
            <dd>{{ meta?.image }}</dd>
            <dt>分辨率</dt>
            <dd>{{ meta?.resolution }} m/px</dd>
            <dt>尺寸</dt>
            <dd>
              {{ grid?.widthPx }}×{{ grid?.heightPx }} px（{{ grid?.widthM.toFixed(1) }}×{{
                grid?.heightM.toFixed(1)
              }}
              m）
            </dd>
            <dt>原点</dt>
            <dd>{{ meta?.origin.x }}, {{ meta?.origin.y }}, {{ meta?.origin.yaw }}</dd>
            <dt>阈值</dt>
            <dd>
              free {{ meta?.free_thresh }} / occ {{ meta?.occupied_thresh }} / negate {{ meta?.negate }}
            </dd>
            <dt>摘要</dt>
            <dd>{{ editor.scene.nav_map.image_sha256.slice(0, 12) }}…</dd>
          </dl>
          <p class="se-meta">可通行性：待验证（无真实规划证据，不默认通过）</p>
        </template>
        <div v-if="issues.length" class="se-issues" data-testid="se-issues">
          <h3>校验结果</h3>
          <p v-for="(i, k) in issues" :key="k" :class="i.level">{{ issueText(i.code) }} · {{ i.ref }}</p>
        </div>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef, triggerRef, watch } from 'vue'
import { MapAdapter, type Point } from '@/lib/map-adapter'
import {
  NavMapGrid,
  bindImage,
  parseMapYaml,
  parseRaster,
  sha256Hex,
  type NavMapMeta,
  type RasterInfo,
} from '@/lib/nav-map'
import {
  SceneEditor,
  makePose,
  newSceneId,
  type Issue,
  type PointKind,
  type ScenePoint,
} from '@/lib/scene-editor'

const kindLabel: Record<PointKind, string> = {
  INSPECTION: '巡检点',
  FIRE_OPERATION: '消防作业点',
  RETURN_HOME: '返航点',
  WAITING: '等待点',
  TRANSIT: '过渡点',
}
const taughtLabel = { MANUAL_INPUT: '精确输入', MAP_CLICK: '地图放置', VEHICLE_POSE: '当前位置示教' }
const issueLabel: Record<string, string> = {
  RETURN_HOME_POINT_MISSING: '缺少返航点',
  POINT_OUTSIDE_NAV_MAP: '点位超出导航地图范围',
  ROUTE_TOO_SHORT: '路线点数不足',
  ROUTE_POINT_REF_MISSING: '路线引用了不存在的点位',
  TRAVERSABILITY_PENDING_VERIFICATION: '可通行性待真实规划验证',
  NAV_MAP_IDENTITY_INCOMPLETE: '导航地图身份不完整',
}

const editor = shallowRef<SceneEditor | null>(null)
const grid = shallowRef<NavMapGrid | null>(null)
const meta = shallowRef<NavMapMeta | null>(null)
const preview = ref('')
const error = ref('')
const issues = ref<Issue[]>([])
const selectedId = ref<string | null>(null)
const addKind = ref<PointKind | null>(null)
const dragId = ref<string | null>(null)
const dragPos = ref<Point | null>(null)
const cursor = ref<Point | null>(null)
const canvasHost = ref<HTMLElement | null>(null)
const vp = reactive({ width: 800, height: 560 })
const adapter = new MapAdapter({ width_m: 1, height_m: 1, origin_x: 0, origin_y: 0, rotation_rad: 0 }, vp)
let observer: ResizeObserver | null = null
let seq = 1

function act(fn: () => void): void {
  try {
    error.value = ''
    fn()
  } catch (e) {
    error.value = `操作被拒绝：${(e as Error).message}`
  }
  triggerRef(editor)
}

const statusText = computed(() =>
  editor.value
    ? `${editor.value.scene.status} · v${editor.value.scene.scene_version}${editor.value.dirty ? ' · 有未保存修改' : ''}`
    : '未加载地图',
)
const groups = computed(() => {
  const s = editor.value?.scene
  return (['INSPECTION', 'FIRE_OPERATION', 'RETURN_HOME'] as PointKind[]).map((kind) => ({
    kind,
    label: kindLabel[kind],
    items: s ? s.points.filter((p) => p.kind === kind) : [],
  }))
})
const selected = computed<ScenePoint | undefined>(() =>
  editor.value?.scene.points.find((p) => p.point_id === selectedId.value),
)
const routeCandidates = computed(
  () => editor.value?.scene.points.filter((p) => p.kind !== 'FIRE_OPERATION') ?? [],
)
const cursorText = computed(() =>
  cursor.value
    ? `x ${cursor.value.x.toFixed(3)} m · y ${cursor.value.y.toFixed(3)} m`
    : '移动鼠标查看世界坐标',
)

// world <-> screen (geometry from the nav map, so screen is drawing only)
function scr(p: Point): Point {
  void vp.width
  void editor.value
  return adapter.worldToScreen(p)
}
/** Screen heading from two world points, so map rotation and the y-flip are both honoured. */
function headingDeg(p: ScenePoint): number {
  const a = scr(posOf(p))
  const b = scr({ x: posOf(p).x + Math.cos(p.pose.yaw), y: posOf(p).y + Math.sin(p.pose.yaw) })
  return (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI
}
function seg(s: { from: Point; to: Point }) {
  const a = scr(s.from)
  const b = scr(s.to)
  return { x1: a.x, y1: a.y, x2: b.x, y2: b.y }
}
const imgBox = computed(() => {
  void vp.width
  if (!grid.value) return { x: 0, y: 0, w: 0, h: 0, t: '' }
  const tl = adapter.worldToScreen(grid.value.pixelToWorld(0, 0))
  const tr = adapter.worldToScreen(grid.value.pixelToWorld(grid.value.widthPx, 0))
  const w = Math.hypot(tr.x - tl.x, tr.y - tl.y)
  const angle = (Math.atan2(tr.y - tl.y, tr.x - tl.x) * 180) / Math.PI
  return {
    x: tl.x,
    y: tl.y,
    w,
    h: w * (grid.value.heightPx / grid.value.widthPx),
    t: `rotate(${angle} ${tl.x} ${tl.y})`,
  }
})

function eventWorld(e: MouseEvent): Point {
  const rect = (e.currentTarget as SVGElement).getBoundingClientRect()
  return adapter.screenToWorld({ x: e.clientX - rect.left, y: e.clientY - rect.top })
}
function onMove(e: PointerEvent): void {
  cursor.value = eventWorld(e)
  if (dragId.value) dragPos.value = { x: round(cursor.value.x), y: round(cursor.value.y) }
}
/** Drag shows a ghost and commits ONE undo step on release. */
function endDrag(): void {
  const id = dragId.value
  const pos = dragPos.value
  dragId.value = null
  dragPos.value = null
  if (id && pos && editor.value) act(() => editor.value!.updatePoint(id, pos))
}
function posOf(p: ScenePoint): Point {
  return p.point_id === dragId.value && dragPos.value ? dragPos.value : p.pose
}
function startDrag(id: string): void {
  selectedId.value = id
  dragId.value = id
}
function round(v: number): number {
  return Math.round(v * 1000) / 1000
}
function onCanvasClick(e: MouseEvent): void {
  if (!editor.value || !addKind.value) return
  const w = eventWorld(e)
  const kind = addKind.value
  const prefix = {
    INSPECTION: 'INSP',
    FIRE_OPERATION: 'FIRE',
    RETURN_HOME: 'HOME',
    WAITING: 'WAIT',
    TRANSIT: 'TR',
  }[kind]
  act(() => {
    selectedId.value = editor.value!.addPoint({
      kind,
      code: `${prefix}-${String(seq++).padStart(2, '0')}`,
      pose: makePose(round(w.x), round(w.y), 0),
      taught_from: 'MAP_CLICK',
    })
  })
  addKind.value = null
}
function num(e: Event): number {
  return Number((e.target as HTMLInputElement).value)
}
function patch(p: Parameters<SceneEditor['updatePoint']>[1]): void {
  if (selected.value) act(() => editor.value!.updatePoint(selected.value!.point_id, p))
}
function addRoute(): void {
  act(() =>
    editor.value!.addRoute(
      `R-${String(seq++).padStart(2, '0')}`,
      routeCandidates.value.map((p) => p.point_id),
    ),
  )
}
function runValidate(): void {
  issues.value = editor.value?.validate() ?? []
}
function issueText(code: string): string {
  return issueLabel[code] ?? code
}

function rasterPreview(raster: RasterInfo, file: File): Promise<string> {
  if (raster.format === 'png') return Promise.resolve(URL.createObjectURL(file))
  const c = document.createElement('canvas')
  c.width = raster.width
  c.height = raster.height
  const ctx = c.getContext('2d')
  if (!ctx || !raster.data) return Promise.resolve('')
  const img = ctx.createImageData(raster.width, raster.height)
  const scale = 255 / (raster.maxval || 255)
  raster.data.forEach((v, i) => {
    const g = v * scale
    img.data.set([g, g, g, 255], i * 4)
  })
  ctx.putImageData(img, 0, 0)
  return Promise.resolve(c.toDataURL('image/png'))
}

async function onImport(e: Event): Promise<void> {
  error.value = ''
  const files = Array.from((e.target as HTMLInputElement).files ?? [])
  const yamlFile = files.find((f) => /\.ya?ml$/i.test(f.name))
  const imageFile = files.find((f) => /\.(pgm|png)$/i.test(f.name))
  try {
    if (!yamlFile || !imageFile) throw new Error('需同时选择 YAML 与 PGM/PNG 两个文件')
    const yamlBytes = new Uint8Array(await yamlFile.arrayBuffer())
    const m = parseMapYaml(new TextDecoder().decode(yamlBytes), yamlBytes.length)
    bindImage(m, imageFile.name)
    const imageBytes = new Uint8Array(await imageFile.arrayBuffer())
    const raster = parseRaster(imageFile.name, imageBytes)
    const g = new NavMapGrid(m, raster.width, raster.height)
    const [imgSha, yamlSha] = await Promise.all([sha256Hex(imageBytes), sha256Hex(yamlBytes)])
    const mapCode = m.image
      .replace(/\.(pgm|png)$/i, '')
      .toUpperCase()
      .replace(/[^A-Z0-9_.-]/g, '_')
    meta.value = m
    grid.value = g
    adapter.setMap(g.geometry())
    adapter.reset()
    preview.value = await rasterPreview(raster, imageFile)
    editor.value = new SceneEditor(
      {
        scene_id: newSceneId(),
        scene_version: 1,
        base_scene_version: null,
        status: 'DRAFT',
        site_code: 'LOCAL_DRAFT',
        nav_map: {
          map_code: mapCode,
          map_version: yamlSha.slice(0, 8),
          frame_id: 'map',
          resolution_m_per_px: m.resolution,
          origin: makePose(m.origin.x, m.origin.y, m.origin.yaw),
          width_px: raster.width,
          height_px: raster.height,
          image_sha256: imgSha,
          yaml_sha256: yamlSha,
          map_checksum: '',
          occupied_thresh: m.occupied_thresh,
          free_thresh: m.free_thresh,
          negate: m.negate,
        },
        points: [],
        parking_slots: [],
        routes: [],
        corridors: [],
      },
      g,
    )
    selectedId.value = null
    issues.value = []
  } catch (err) {
    error.value = `导入被拒绝：${(err as Error).message}`
  }
}

function onKey(e: KeyboardEvent): void {
  if (!editor.value || (e.target as HTMLElement)?.tagName === 'INPUT') return
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z')
    act(() => (e.shiftKey ? editor.value!.redo() : editor.value!.undo()))
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  observer = new ResizeObserver(([entry]) => {
    vp.width = Math.max(320, Math.floor(entry.contentRect.width))
    vp.height = Math.max(320, Math.floor(entry.contentRect.height))
    adapter.setViewport({ ...vp })
    triggerRef(editor)
  })
})
// The canvas only exists after a map is imported (v-if), so observe it when it appears.
watch(canvasHost, (el, prev) => {
  if (prev) observer?.unobserve(prev)
  if (el) observer?.observe(el)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  observer?.disconnect()
})
</script>

<style scoped>
.scene-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: calc(100vh - var(--yd-topbar-height) - 32px);
  min-height: 520px;
}
.se-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.se-title {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.se-title h1 {
  margin: 0;
  font-size: 20px;
  color: var(--yd-text);
}
.se-status {
  padding: 2px 8px;
  border-radius: var(--yd-radius-sm);
  background: var(--yd-primary-soft);
  color: var(--yd-primary);
  font-size: 13px;
}
.se-note {
  color: var(--yd-text-2);
  font-size: 13px;
}
.se-actions {
  display: flex;
  gap: 8px;
}
.se-actions button,
.se-file,
.se-group-head button,
.se-inspector button {
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid var(--yd-border-strong);
  border-radius: var(--yd-radius-sm);
  background: var(--yd-surface);
  color: var(--yd-text);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
}
.se-actions button:disabled {
  color: var(--yd-text-3);
  cursor: not-allowed;
}
.se-file input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}
.se-file:focus-within,
button:focus-visible,
input:focus-visible {
  outline: 2px solid var(--yd-primary);
  outline-offset: 2px;
}
.se-error {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid var(--yd-danger-border);
  background: var(--yd-danger-soft);
  color: var(--yd-danger);
  border-radius: var(--yd-radius-sm);
}
.se-empty {
  padding: 32px;
  border: 1px dashed var(--yd-border-strong);
  border-radius: var(--yd-radius-md);
  color: var(--yd-text-2);
  background: var(--yd-surface);
}
.se-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr) 280px;
  gap: 12px;
}
.se-tree,
.se-inspector {
  overflow: auto;
  background: var(--yd-surface);
  border: 1px solid var(--yd-border);
  border-radius: var(--yd-radius-md);
  padding: 12px;
}
.se-group + .se-group {
  margin-top: 14px;
}
.se-group-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--yd-text);
  font-size: 14px;
  margin-bottom: 6px;
}
.se-group-head button {
  min-height: 30px;
  padding: 0 8px;
  font-size: 13px;
}
.se-group-head button.active {
  border-color: var(--yd-primary);
  color: var(--yd-primary);
  background: var(--yd-primary-soft);
}
.se-item {
  display: flex;
  width: 100%;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  border: 0;
  background: none;
  border-radius: var(--yd-radius-sm);
  color: var(--yd-text);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.se-item.selected {
  background: var(--yd-primary-soft);
  color: var(--yd-primary);
}
.se-item.static {
  cursor: default;
}
.se-item .link {
  border: 0;
  background: none;
  color: var(--yd-primary);
  cursor: pointer;
  min-height: 0;
  padding: 0;
}
.num {
  font-variant-numeric: tabular-nums;
  color: var(--yd-text-2);
}
.se-canvas {
  position: relative;
  min-width: 0;
  min-height: 0;
  background: var(--yd-surface-soft);
  border: 1px solid var(--yd-border);
  border-radius: var(--yd-radius-md);
  overflow: hidden;
}
.se-canvas image,
.se-point line,
.se-point text,
.se-route {
  pointer-events: none;
}
.se-canvas svg {
  position: absolute;
  inset: 0;
  display: block;
  cursor: crosshair;
}
.se-cursor {
  position: absolute;
  left: 12px;
  bottom: 10px;
  padding: 2px 8px;
  background: var(--yd-surface);
  border: 1px solid var(--yd-border);
  border-radius: var(--yd-radius-sm);
  font-size: 12px;
}
.se-route {
  stroke: var(--yd-primary);
  stroke-width: 2;
  stroke-dasharray: 6 4;
}
.se-arrow {
  fill: var(--yd-primary);
}
.se-point {
  cursor: grab;
}
.se-point circle {
  fill: var(--yd-surface);
  stroke: var(--yd-primary);
  stroke-width: 2.5;
}
.se-point line {
  stroke: var(--yd-primary);
  stroke-width: 2.5;
}
.se-point text {
  fill: var(--yd-text);
  font-size: 12px;
  transform: none;
}
.se-point.FIRE_OPERATION circle,
.se-point.FIRE_OPERATION line {
  stroke: var(--yd-danger);
}
.se-point.RETURN_HOME circle,
.se-point.RETURN_HOME line {
  stroke: var(--yd-success);
}
.se-point.selected circle {
  fill: var(--yd-primary-soft);
  stroke-width: 4;
}
.se-inspector h2 {
  margin: 0 0 10px;
  font-size: 16px;
  color: var(--yd-text);
}
.se-inspector label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--yd-text-2);
}
.se-inspector input {
  min-height: 34px;
  padding: 0 8px;
  border: 1px solid var(--yd-border-strong);
  border-radius: var(--yd-radius-sm);
  font: inherit;
  color: var(--yd-text);
  font-variant-numeric: tabular-nums;
}
.se-inspector .danger {
  color: var(--yd-danger);
  border-color: var(--yd-danger-border);
}
.se-meta {
  font-size: 12px;
  color: var(--yd-text-2);
}
.se-meta-list {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 10px;
  font-size: 13px;
  margin: 0;
}
.se-meta-list dt {
  color: var(--yd-text-3);
}
.se-meta-list dd {
  margin: 0;
  color: var(--yd-text);
  word-break: break-all;
}
.se-issues {
  margin-top: 14px;
  border-top: 1px solid var(--yd-border-soft);
  padding-top: 10px;
}
.se-issues h3 {
  margin: 0 0 6px;
  font-size: 14px;
}
.se-issues p {
  margin: 2px 0;
  font-size: 13px;
}
.se-issues .error {
  color: var(--yd-danger);
}
.se-issues .warning {
  color: var(--yd-warning);
}
@media (max-width: 1400px) {
  .se-body {
    grid-template-columns: 200px minmax(0, 1fr) 240px;
  }
}
</style>

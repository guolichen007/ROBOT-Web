// Centralized Chinese labels for operator-facing raw enum values.
// API enums are NOT changed; only display text is localized here.

const map =
  <T extends string>(table: Record<string, string>, fallback: string) =>
  (v?: T | null): string =>
    table[String(v ?? '').toUpperCase()] || fallback

export const alarmTypeLabel = (v?: string): string =>
  ({
    smoke: '烟雾',
    flame: '明火',
    thermal: '热异常',
    unknown: '未知火情',
  })[String(v || '').toLowerCase()] || '其它类型'

export const severityLabel = map(
  { CRITICAL: '严重', HIGH: '高', MEDIUM: '中', LOW: '低' },
  '未知',
)

export const alarmStateLabel = map(
  {
    NEW: '新告警',
    ACKNOWLEDGED: '已收到',
    CONFIRMED: '已确认',
    DISPATCHED: '已派发',
    IN_PROGRESS: '处置中',
    RESOLVED: '已解决',
    DISMISSED: '已关闭',
    CLOSED: '已关闭',
  },
  '未知状态',
)

export const supportStateLabel = map(
  {
    CONNECTED: '正常',
    STALE: '数据陈旧',
    NOT_CONNECTED: '未接入',
    ERROR: '异常',
    UNSUPPORTED: '当前车型不支持',
  },
  '未知',
)

export const streamStateLabel = map(
  {
    LIVE: '实时',
    CONNECTING: '连接中',
    OFFLINE: '离线',
    ERROR: '异常',
    DISABLED: '已停用',
  },
  '未知',
)

export const detectionMethodLabel = map(
  { AUTO: '自动检测', MANUAL: '人工上报' },
  '未知方式',
)

export const taskStateLabel = map(
  {
    CREATED: '已创建',
    QUEUED: '等待执行',
    ACCEPTED: '已接受',
    EXECUTING: '执行中',
    SUCCEEDED: '已完成',
    FAILED: '失败',
    CANCELLED: '已取消',
  },
  '未知',
)

export const localizationLabel = map(
  { OK: '正常', GOOD: '良好', VALID: '有效', VALID_SOURCE: '有效', DEGRADED: '降级' },
  '未知',
)

export const taskTypeLabel = map(
  { PATROL: '巡检', EXTINGUISH: '灭火', NAVIGATE: '导航', RETURN_DOCK: '返回待命区' },
  '任务',
)

export const sourceTypeLabel = map(
  { ALARM: '告警', TASK: '任务', COMMAND: '命令', OPERATION: '操作' },
  '其它',
)

// Generic chip label covering the common fleet/alarm/map/user/audit states.
const CHIP_LABELS: Record<string, string> = {
  ONLINE: '在线',
  STALE: '数据陈旧',
  OFFLINE: '离线',
  READY: '就绪',
  LIVE: '实时',
  SUCCEEDED: '已完成',
  ACCEPTED: '已接受',
  PUBLISHED: '已发布',
  RESOLVED: '已解决',
  CLOSED: '已关闭',
  DISMISSED: '已关闭',
  ACTIVE: '启用',
  ENABLED: '启用',
  DISABLED: '已停用',
  CONNECTED: '正常',
  NOT_CONNECTED: '未接入',
  UNSUPPORTED: '不支持',
  QUEUED: '等待执行',
  EXECUTING: '执行中',
  ACKNOWLEDGED: '已收到',
  CONNECTING: '连接中',
  CREATED: '已创建',
  DEGRADED: '降级',
  PARTIAL: '部分',
  IN_PROGRESS: '处置中',
  DISPATCHED: '已派发',
  PENDING: '待处理',
  DRAFT: '草稿',
  ARCHIVED: '已归档',
  MEDIUM: '中',
  FAILED: '失败',
  ERROR: '异常',
  UNCONFIRMED: '未确认',
  PUBLISHED_UNCONFIRMED: '未确认',
  NEW: '新告警',
  CONFIRMED: '已确认',
  CRITICAL: '严重',
  HIGH: '高',
  LOW: '低',
  CANCELLED: '已取消',
  OK: '正常',
  GOOD: '良好',
  VALID: '有效',
  VALID_SOURCE: '有效',
}

export function stateChipLabel(value: string): string {
  return CHIP_LABELS[String(value || '').toUpperCase()] || value
}

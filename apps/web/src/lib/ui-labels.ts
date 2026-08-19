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
  {
    PATROL: '巡检任务',
    EXTINGUISH: '灭火任务',
    NAVIGATE: '导航任务',
    NAVIGATE_TO_PRESET: '前往预设点',
    RETURN_DOCK: '返回待命区',
  },
  '其它任务',
)

export const sourceTypeLabel = map(
  { ALARM: '告警', TASK: '任务', COMMAND: '命令', OPERATION: '操作' },
  '其它',
)

export const robotModeLabel = map(
  {
    IDLE: '待命',
    STANDBY: '待命',
    PATROLLING: '巡检中',
    EXTINGUISHING: '灭火中',
    RETURNING: '返航中',
    ESTOP: '急停',
  },
  '未知',
)

export const situationLabel = map(
  {
    FIRE_CRITICAL: '严重火情',
    ESTOP_ACTIVE: '软件急停已生效',
    DEGRADED: '系统降级',
    NORMAL: '正常',
    OFFLINE_UNKNOWN: '现场态势未知',
  },
  '系统异常',
)

export const reasonCodeLabel = (v?: string): string =>
  ({
    ROBOT_ESTOP_ACTIVE: '机器人处于急停状态',
    ROBOT_OFFLINE: '机器人离线',
    CONTROL_NOT_READY: '控制链路尚未就绪',
    ACTIVE_TASK_EXISTS: '当前已有执行中任务',
    ACTIVE_TASK_CONFLICT: '当前已有执行中任务',
    READ_ONLY_INTEGRATION: '当前为只读接入，控制未开放',
    INTEGRATION_PROFILE_MISSING: '缺少控制集成配置',
    CAPABILITY_DECLARATION_MISSING: '车辆能力声明缺失',
    EXTINGUISH_MODE_REQUIRED: '灭火任务必须明确选择处理方式',
  })[String(v || '').toUpperCase()] || ''

export const auditActionLabel = (v?: string): string =>
  ({
    AUTH_LOGIN: '用户登录',
    AUTH_LOGOUT: '用户退出',
    EMERGENCY_STOP: '触发急停',
    RESET_ESTOP: '急停复位',
    PATROL_STOP_REQUEST: '请求停止巡检',
    TASK_PATROL_CREATE: '创建巡检任务',
    TASK_EXTINGUISH_CREATE: '创建灭火任务',
    ALARM_MANUAL_CREATE: '人工创建火情',
    ALARM_ACKNOWLEDGE: '确认收到火情',
    ALARM_CONFIRM: '确认火情',
    ALARM_RESOLVE: '解除火情',
    ALARM_RESOLVED: '火情已解除',
    RETURN_DOCK: '返回待命区',
    MANUAL_LEASE_ACQUIRE: '获取手动控制租约',
    MANUAL_LEASE_RELEASE: '释放手动控制租约',
    MAP_PUBLISH: '发布地图版本',
    MAP_ARCHIVE: '归档地图版本',
    USER_CREATE: '创建用户',
  })[String(v || '').toUpperCase()] || String(v || '')

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

import { api } from '@/lib/api'

export const ROLE_LABELS: Record<string, string> = {
  super_admin: '超级管理员',
  administrator: '管理员',
  dispatcher: '调度员',
  operator: '操作员',
  viewer: '只读用户',
  auditor: '审计员',
}

export const roleLabel = (code: string): string => ROLE_LABELS[code] || code

export const WEEKDAY_OPTIONS = [
  { value: 'MON', label: '一' },
  { value: 'TUE', label: '二' },
  { value: 'WED', label: '三' },
  { value: 'THU', label: '四' },
  { value: 'FRI', label: '五' },
  { value: 'SAT', label: '六' },
  { value: 'SUN', label: '日' },
]

export interface ScheduleSpec {
  frequency: 'DAILY' | 'WEEKLY'
  time: string
  weekdays: string[]
  timezone: string
}

export function scheduleLabel(spec?: Partial<ScheduleSpec> | null): string {
  if (!spec || !spec.frequency) return '未设置'
  if (spec.frequency === 'DAILY') return `每天 ${spec.time}`
  const days = (spec.weekdays || [])
    .map((d) => WEEKDAY_OPTIONS.find((o) => o.value === d)?.label || d)
    .join('、')
  return `每周${days} ${spec.time}`
}

/** 去掉空值后作为查询参数；导出与页面查询共用，保证导出内容 = 页面筛选结果。 */
export function cleanParams(input: Record<string, unknown>): Record<string, string> {
  const result: Record<string, string> = {}
  for (const [key, value] of Object.entries(input)) {
    if (value === undefined || value === null || value === '') continue
    if (key === 'start' || key === 'end') {
      const date = new Date(String(value))
      if (!Number.isNaN(date.getTime())) {
        if (key === 'end' && /^\d{4}-\d{2}-\d{2}$/.test(String(value))) date.setHours(23, 59, 59, 999)
        result[key] = date.toISOString()
      }
      continue
    }
    result[key] = String(value)
  }
  return result
}

export async function exportCsv(kind: string, params: Record<string, unknown>): Promise<void> {
  const response = await api.get(`/history/export/${kind}`, {
    params: cleanParams(params),
    responseType: 'blob',
  })
  const disposition = String(response.headers['content-disposition'] || '')
  const match = /filename="([^"]+)"/.exec(disposition)
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = match?.[1] || `firebot-${kind}.csv`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

export const canExport = (permissions: string[] | undefined, kind: string): boolean => {
  const set = new Set(permissions || [])
  const base: Record<string, string> = {
    tasks: 'robot.read',
    'plan-previews': 'robot.read',
    alarms: 'alarm.read',
    operations: 'audit.read',
    audit: 'audit.read',
  }
  const extra = ['patrol.create', 'alarm.ack', 'audit.read']
  return set.has(base[kind] || '__none__') && extra.some((p) => set.has(p))
}

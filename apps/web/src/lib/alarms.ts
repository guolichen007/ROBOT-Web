import type { Alarm } from '@/types'

export const ACTIVE_ALARM_STATES = new Set(['NEW', 'ACKNOWLEDGED', 'CONFIRMED', 'DISPATCHED', 'IN_PROGRESS'])

export const isActiveAlarm = (alarm: Pick<Alarm, 'state'>): boolean => ACTIVE_ALARM_STATES.has(alarm.state)
export const isUnacknowledged = (alarm: Pick<Alarm, 'state'>): boolean => alarm.state === 'NEW'

export function alarmLocation(alarm: Partial<Alarm>): string {
  if (alarm.location_label) return alarm.location_label
  if (alarm.location_state === 'UNMATCHED' || !alarm.parking_slot_id)
    return `位置未匹配（车端上报：${alarm.reported_slot_code || '无车位编码'}）`
  return alarm.parking_slot_code || '已匹配车位'
}

/**
 * 弹窗只针对“未确认收到”的火警；“关闭提示”仅隐藏本机弹窗（不改变火警状态、不写审计），
 * 与“确认收到”（服务端状态迁移 + 审计）严格区分。
 */
export function popupQueue(alarms: Alarm[], dismissed: Set<string>): Alarm[] {
  return alarms
    .filter((alarm) => isUnacknowledged(alarm) && !dismissed.has(`${alarm.id}:${alarm.occurrence_count}`))
    .sort((a, b) => new Date(b.last_seen_at).getTime() - new Date(a.last_seen_at).getTime())
}

/**
 * 火情检测数据状态（WEB-R1-MIN）：只有存在“实时接入”的检测通道（smoke / flame / top_ir / bottom_ir
 * 任一 CONNECTED）时才可能显示“暂无火警”；没有真实检测数据一律显示“检测数据未接入”，
 * 绝不显示“监测正常”。
 */
export type DetectionDataState = 'ALARM' | 'NO_ALARM_LIVE' | 'NO_DATA'

const DETECTION_CHANNELS = ['smoke', 'flame', 'top_ir', 'bottom_ir']

export function detectionDataState(
  robots: Array<{ online_state?: string; data_channels?: Record<string, { support_state?: string }> }>,
  activeAlarmCount: number,
): DetectionDataState {
  if (activeAlarmCount > 0) return 'ALARM'
  const live = robots.some(
    (robot) =>
      robot.online_state === 'ONLINE' &&
      DETECTION_CHANNELS.some((channel) => robot.data_channels?.[channel]?.support_state === 'CONNECTED'),
  )
  return live ? 'NO_ALARM_LIVE' : 'NO_DATA'
}

export const detectionDataLabel = (state: DetectionDataState, count: number): string =>
  state === 'ALARM' ? `${count} 条活动火警` : state === 'NO_ALARM_LIVE' ? '暂无活动火警' : '检测数据未接入'

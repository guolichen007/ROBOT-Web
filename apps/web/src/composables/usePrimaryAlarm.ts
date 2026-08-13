import { computed, ref, watch, type Ref } from 'vue'
import { compareOperationalAlarms } from '@/lib/operations'
import type { Alarm } from '@/types'

const ACTIVE = new Set(['NEW', 'ACKNOWLEDGED', 'CONFIRMED', 'DISPATCHED', 'IN_PROGRESS'])
const severity = (value: string) => ({ CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1 })[value] || 0

export function usePrimaryAlarm(alarms: Ref<Alarm[]>) {
  const primaryAlarmId = ref<string | null>(null)
  const activeAlarms = computed(() =>
    alarms.value.filter((item) => ACTIVE.has(item.state)).sort(compareOperationalAlarms),
  )
  const primaryAlarm = computed(() => activeAlarms.value.find((item) => item.id === primaryAlarmId.value))
  watch(
    activeAlarms,
    (items) => {
      const current = items.find((item) => item.id === primaryAlarmId.value)
      if (!current) primaryAlarmId.value = items[0]?.id || null
      else if (items[0] && severity(items[0].severity) > severity(current.severity))
        primaryAlarmId.value = items[0].id
    },
    { immediate: true },
  )
  return { activeAlarms, primaryAlarm, primaryAlarmId }
}

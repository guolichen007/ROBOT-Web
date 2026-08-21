import { computed, effectScope, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { usePrimaryAlarm } from '@/composables/usePrimaryAlarm'
import { useHoldToConfirm } from '@/composables/useHoldToConfirm'
import type { Alarm } from '@/types'

const alarm = (id: string, severity: string, state = 'NEW', last = id): Alarm => ({
  id,
  robot_id: 'robot-a',
  event_code: id,
  state,
  severity,
  fire_type: 'smoke',
  occurrence_count: 1,
  detection_method: 'AUTO',
  last_seen_at: last,
})

describe('commercial HMI safety behavior', () => {
  it('keeps sticky primary for same severity and promotes higher severity', async () => {
    const rows = ref([alarm('A', 'HIGH', 'NEW', '2026-08-13T00:00:00Z')])
    const result = effectScope().run(() => usePrimaryAlarm(computed(() => rows.value)))!
    await nextTick()
    expect(result.primaryAlarmId.value).toBe('A')
    rows.value = [alarm('B', 'HIGH', 'NEW', '2026-08-13T00:01:00Z'), ...rows.value]
    await nextTick()
    expect(result.primaryAlarmId.value).toBe('A')
    rows.value = [alarm('C', 'CRITICAL', 'NEW', '2026-08-13T00:02:00Z'), ...rows.value]
    await nextTick()
    expect(result.primaryAlarmId.value).toBe('C')
    rows.value = rows.value.map((item) => (item.id === 'C' ? { ...item, state: 'RESOLVED' } : item))
    await nextTick()
    expect(result.primaryAlarmId.value).toBe('B')
  })

  it('hold-to-confirm sends once only after 800ms', () => {
    vi.useFakeTimers()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 16),
    )
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
    const action = vi.fn()
    const scope = effectScope()
    const hold = scope.run(() => useHoldToConfirm(action, 800))!
    hold.start()
    vi.advanceTimersByTime(500)
    hold.cancel()
    expect(action).not.toHaveBeenCalled()
    hold.start()
    vi.advanceTimersByTime(850)
    expect(action).toHaveBeenCalledTimes(1)
    scope.stop()
    vi.useRealTimers()
  })
})

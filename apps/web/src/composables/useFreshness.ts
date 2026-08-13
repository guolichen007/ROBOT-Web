import { computed, type Ref } from 'vue'

export function useFreshness(timestamp: Ref<string | null | undefined>) {
  return computed(() => {
    if (!timestamp.value) return { state: 'NOT_CONNECTED', label: '无数据' }
    const seconds = (Date.now() - Date.parse(timestamp.value)) / 1000
    if (!Number.isFinite(seconds)) return { state: 'ERROR', label: '时间无效' }
    return seconds > 3
      ? { state: 'STALE', label: `${Math.round(seconds)} 秒前` }
      : { state: 'CONNECTED', label: `${Math.max(0, seconds).toFixed(1)} 秒前` }
  })
}

import { onUnmounted, ref } from 'vue'

export function useSystemClock(): { now: ReturnType<typeof ref<Date>> } {
  const now = ref(new Date())
  const timer = window.setInterval(() => (now.value = new Date()), 1000)
  onUnmounted(() => window.clearInterval(timer))
  return { now }
}

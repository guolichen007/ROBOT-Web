import { getCurrentInstance, onUnmounted, ref } from 'vue'

export function useHoldToConfirm(action: () => void | Promise<void>, duration = 800) {
  const progress = ref(0)
  let frame = 0
  let started = 0
  let fired = false
  const cancel = () => {
    cancelAnimationFrame(frame)
    progress.value = 0
    started = 0
    fired = false
  }
  const tick = () => {
    progress.value = Math.min(100, ((performance.now() - started) / duration) * 100)
    if (progress.value >= 100 && !fired) {
      fired = true
      void action()
      cancelAnimationFrame(frame)
      return
    }
    frame = requestAnimationFrame(tick)
  }
  const start = () => {
    if (started) return
    started = performance.now()
    frame = requestAnimationFrame(tick)
  }
  window.addEventListener('blur', cancel)
  document.addEventListener('visibilitychange', cancel)
  if (getCurrentInstance()) {
    onUnmounted(() => {
      cancel()
      window.removeEventListener('blur', cancel)
      document.removeEventListener('visibilitychange', cancel)
    })
  }
  return { progress, start, cancel }
}

import { ref, shallowRef, type Ref } from 'vue'
import { errorMessage } from '@/lib/api'

/** 通用列表加载：统一 loading / error / 数据三态，供 DataTable 使用。 */
export function useAsyncList<T = Record<string, any>>(loader: () => Promise<T[]>) {
  const rows: Ref<T[]> = shallowRef<T[]>([])
  const loading = ref(false)
  const error = ref('')
  let generation = 0

  async function load(): Promise<void> {
    const current = ++generation
    loading.value = true
    error.value = ''
    try {
      const data = await loader()
      if (current === generation) rows.value = data
    } catch (reason) {
      if (current === generation) error.value = errorMessage(reason)
    } finally {
      if (current === generation) loading.value = false
    }
  }

  return { rows, loading, error, load }
}

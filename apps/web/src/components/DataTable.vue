<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    columns: Array<{ key: string; label: string }>
    rows: Array<Record<string, any>>
    empty?: string
    loading?: boolean
    error?: string
    pageSize?: number
  }>(),
  { empty: '', loading: false, error: '', pageSize: 20 },
)
const emit = defineEmits<{ retry: [] }>()

const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil(props.rows.length / props.pageSize)))
const pagedRows = computed(() =>
  props.rows.slice((page.value - 1) * props.pageSize, page.value * props.pageSize),
)
// 数据集变化（筛选 / 刷新）后回到首页，避免停在已不存在的页码。
watch(
  () => props.rows,
  () => {
    if (page.value > pageCount.value) page.value = 1
  },
)

function valueAt(row: Record<string, any>, key: string): unknown {
  const value = key.split('.').reduce<any>((current, part) => current?.[part], row)
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value))
    return new Date(value).toLocaleString('zh-CN')
  return value
}
</script>

<template>
  <div class="table-wrap" :aria-busy="loading">
    <table>
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
        </tr>
      </thead>
      <tbody v-if="!loading && !error">
        <tr v-for="row in pagedRows" :key="String(row.id || row.code || row.key)">
          <td v-for="column in columns" :key="column.key" :data-label="column.label">
            <slot :name="column.key" :row="row" :value="valueAt(row, column.key)">{{
              valueAt(row, column.key)
            }}</slot>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="loading" class="empty-state" data-state="loading" role="status">
      <strong>正在加载…</strong>
    </div>
    <div v-else-if="error" class="empty-state error" data-state="error" role="alert">
      <strong>加载失败：{{ error }}</strong>
      <button type="button" class="table-button" @click="emit('retry')">重试</button>
    </div>
    <div v-else-if="!rows.length" class="empty-state" data-state="empty">
      <strong>{{ empty || '暂无记录' }}</strong>
    </div>
    <nav v-if="!loading && !error && rows.length > pageSize" class="table-pager" aria-label="分页">
      <span>共 {{ rows.length }} 条 · 第 {{ page }} / {{ pageCount }} 页</span>
      <button type="button" class="table-button" :disabled="page <= 1" @click="page -= 1">上一页</button>
      <button type="button" class="table-button" :disabled="page >= pageCount" @click="page += 1">
        下一页
      </button>
    </nav>
  </div>
</template>

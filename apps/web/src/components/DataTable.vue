<script setup lang="ts">
defineProps<{
  columns: Array<{ key: string; label: string }>
  rows: Array<Record<string, any>>
  empty?: string
}>()

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
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id || row.code || row.key)">
          <td v-for="column in columns" :key="column.key">
            <slot :name="column.key" :row="row" :value="valueAt(row, column.key)">{{
              valueAt(row, column.key)
            }}</slot>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="!rows.length" class="empty-state">
      <strong>{{ empty || '暂无记录' }}</strong>
    </div>
  </div>
</template>

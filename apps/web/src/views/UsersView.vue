<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api, errorMessage } from '@/lib/api'

const rows = ref<any[]>([]),
  roles = ref<any[]>([]),
  showForm = ref(false),
  notice = ref('')
const form = reactive({ username: '', display_name: '', password: '', role_codes: ['viewer'] })
async function load(): Promise<void> {
  ;[rows.value, roles.value] = await Promise.all([
    api.get('/admin/users').then((r) => r.data),
    api.get('/admin/roles').then((r) => r.data),
  ])
}
async function create(): Promise<void> {
  try {
    await api.post('/admin/users', form)
    showForm.value = false
    await load()
    notice.value = '用户已创建，首次登录必须修改密码'
  } catch (error) {
    notice.value = errorMessage(error)
  }
}
onMounted(load)
</script>

<template>
  <PageHeader eyebrow="IDENTITY / RBAC" title="用户与权限" description="六类角色与高风险权限保持独立授权。"
    ><button class="primary-button compact" @click="showForm = true">新建用户</button></PageHeader
  >
  <p v-if="notice" class="inline-notice">{{ notice }}</p>
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :columns="[
        { key: 'username', label: '账号' },
        { key: 'display_name', label: '显示名称' },
        { key: 'status', label: '状态' },
        { key: 'must_change_password', label: '强制改密' },
        { key: 'last_login_at', label: '最后登录' },
      ]"
    >
      <template #status="{ value }"><StateChip :value="String(value)" /></template
      ><template #must_change_password="{ value }">{{ value ? '是' : '否' }}</template>
    </DataTable>
  </section>
  <div v-if="showForm" class="modal-shade" @click.self="showForm = false">
    <form class="modal-card user-form" @submit.prevent="create">
      <span class="eyebrow">CREATE USER</span>
      <h2>新建平台用户</h2>
      <label>账号<input v-model="form.username" required /></label
      ><label>显示名称<input v-model="form.display_name" required /></label
      ><label>初始密码<input v-model="form.password" type="password" minlength="12" required /></label
      ><label
        >角色<select v-model="form.role_codes[0]">
          <option v-for="role in roles" :key="role.id" :value="role.code">{{ role.name }}</option>
        </select></label
      ><button class="primary-button" type="submit">创建</button
      ><button type="button" class="modal-close" @click="showForm = false">×</button>
    </form>
  </div>
</template>

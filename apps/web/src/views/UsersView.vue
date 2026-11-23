<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { useAsyncList } from '@/composables/useAsyncList'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { cleanParams, roleLabel, ROLE_LABELS } from '@/lib/records'
import { useAuthStore } from '@/stores/auth'

interface UserRow {
  id: string
  username: string
  display_name: string
  status: string
  roles: string[]
  must_change_password: boolean
  last_login_at: string | null
  deactivated_at: string | null
}

const auth = useAuthStore()
const filters = reactive({ q: '', status: '', role: '' })
const { rows, loading, error, load } = useAsyncList<UserRow>(
  async () => (await api.get('/admin/users', { params: cleanParams(filters) })).data,
)
const notice = ref('')
const noticeTheme = ref<'ok' | 'error'>('ok')
const busy = ref(false)
type Mode = 'create' | 'edit' | 'roles' | 'reset' | 'status' | ''
const mode = ref<Mode>('')
const target = ref<UserRow | null>(null)
const form = reactive({
  username: '',
  display_name: '',
  password: '',
  role_codes: ['operator'] as string[],
  reason: '',
})
let createKey = ''

const isSuper = computed(() => Boolean(auth.user?.roles.includes('super_admin')))
const canAssignRoles = computed(() => auth.can('role.manage'))
// 与服务端规则一致：super_admin 只能由 super_admin 授予（前端只做提示，服务端强制）。
const grantableRoles = computed(() =>
  Object.keys(ROLE_LABELS).filter((code) => code !== 'super_admin' || isSuper.value),
)

function say(message: string, theme: 'ok' | 'error' = 'ok'): void {
  notice.value = message
  noticeTheme.value = theme
}

function open(next: Mode, row?: UserRow): void {
  mode.value = next
  target.value = row || null
  form.username = ''
  form.display_name = row?.display_name || ''
  form.password = ''
  form.reason = ''
  form.role_codes = row ? [...row.roles] : ['operator']
  if (next === 'create') createKey = newUuid()
}

function close(): void {
  mode.value = ''
  target.value = null
}

function isSelf(row: UserRow): boolean {
  return row.id === auth.user?.id
}

function toggleRole(code: string): void {
  form.role_codes = form.role_codes.includes(code)
    ? form.role_codes.filter((item) => item !== code)
    : [...form.role_codes, code]
}

async function submit(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    if (mode.value === 'create') {
      await api.post(
        '/admin/users',
        {
          username: form.username,
          display_name: form.display_name,
          password: form.password,
          role_codes: form.role_codes,
        },
        { headers: { 'Idempotency-Key': createKey } },
      )
      say(`账号 ${form.username} 已创建；首次登录必须修改初始密码`)
    } else if (mode.value === 'edit' && target.value) {
      await api.patch(`/admin/users/${target.value.id}`, { display_name: form.display_name })
      say('资料已保存')
    } else if (mode.value === 'roles' && target.value) {
      if (!form.role_codes.length) throw new Error('至少选择一个角色')
      const { data } = await api.put(`/admin/users/${target.value.id}/roles`, {
        role_codes: form.role_codes,
      })
      say(data.sessions_revoked ? '角色已更新，该账号的已登录会话已失效，需重新登录' : '角色未变化')
    } else if (mode.value === 'reset' && target.value) {
      await api.post(`/admin/users/${target.value.id}/reset-password`, {
        new_password: form.password,
      })
      say('密码已重置；该账号下次登录必须修改密码，旧会话已失效')
    } else if (mode.value === 'status' && target.value) {
      const next = target.value.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
      await api.put(`/admin/users/${target.value.id}/status`, { status: next, reason: form.reason })
      say(next === 'DISABLED' ? '账号已停用，所有会话立即失效（历史记录保留）' : '账号已启用')
    }
    close()
    await load()
  } catch (reason) {
    say(errorMessage(reason), 'error')
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <PageHeader
    eyebrow="用户与权限"
    title="用户与权限"
    description="固定六类角色；停用为逻辑停用，历史记录保留。"
    ><button class="primary-button compact" type="button" data-testid="user-create" @click="open('create')">
      新增账号
    </button></PageHeader
  >
  <p v-if="notice" class="inline-notice" :data-theme="noticeTheme" role="status">{{ notice }}</p>
  <form class="filter-bar" @submit.prevent="load">
    <label>关键字<input v-model="filters.q" placeholder="账号 / 显示名称" /></label>
    <label
      >状态<select v-model="filters.status">
        <option value="">全部</option>
        <option value="ACTIVE">启用</option>
        <option value="DISABLED">已停用</option>
      </select></label
    >
    <label
      >角色<select v-model="filters.role">
        <option value="">全部</option>
        <option v-for="(label, code) in ROLE_LABELS" :key="code" :value="code">{{ label }}</option>
      </select></label
    >
    <div class="filter-actions"><button class="secondary-button compact" type="submit">查询</button></div>
  </form>
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :loading="loading"
      :error="error"
      empty="没有符合条件的账号"
      :columns="[
        { key: 'username', label: '账号' },
        { key: 'display_name', label: '显示名称' },
        { key: 'roles', label: '角色' },
        { key: 'status', label: '状态' },
        { key: 'must_change_password', label: '待改密' },
        { key: 'last_login_at', label: '最后登录' },
        { key: 'actions', label: '操作' },
      ]"
      @retry="load"
    >
      <template #roles="{ row }">{{ (row.roles as string[]).map(roleLabel).join('、') || '—' }}</template>
      <template #status="{ value }"><StateChip :value="String(value)" /></template>
      <template #must_change_password="{ value }">{{ value ? '是' : '否' }}</template>
      <template #actions="{ row }"
        ><div class="table-actions">
          <button type="button" @click="open('edit', row as UserRow)">编辑</button>
          <button
            v-if="canAssignRoles && !isSelf(row as UserRow)"
            type="button"
            @click="open('roles', row as UserRow)"
          >
            分配角色</button
          ><button v-if="!isSelf(row as UserRow)" type="button" @click="open('reset', row as UserRow)">
            重置密码</button
          ><button
            v-if="!isSelf(row as UserRow)"
            type="button"
            :data-testid="`user-status-${row.username}`"
            @click="open('status', row as UserRow)"
          >
            {{ row.status === 'ACTIVE' ? '停用' : '启用' }}
          </button>
          <span v-if="isSelf(row as UserRow)">当前账号</span>
        </div></template
      >
    </DataTable>
  </section>

  <div v-if="mode" class="drawer-shade" @click.self="close">
    <form class="drawer-card user-form" data-testid="user-drawer" @submit.prevent="submit">
      <button type="button" class="modal-close" aria-label="关闭" @click="close">×</button>
      <template v-if="mode === 'create'">
        <h2>新增账号</h2>
        <label
          >账号<input
            v-model="form.username"
            name="username"
            required
            pattern="[A-Za-z0-9_.\-]{3,64}"
            autocomplete="off"
          /><span class="form-hint">3–64 位字母、数字、下划线、点或短横线</span></label
        >
        <label
          >显示名称<input v-model="form.display_name" name="display_name" required maxlength="128"
        /></label>
        <label
          >初始密码<input
            v-model="form.password"
            name="password"
            type="password"
            minlength="12"
            required
            autocomplete="new-password"
          /><span class="form-hint"
            >至少 12 位，含大写、小写、数字、符号中的三类；首次登录强制修改</span
          ></label
        >
      </template>
      <template v-else-if="mode === 'edit'">
        <h2>编辑资料 · {{ target?.username }}</h2>
        <label
          >显示名称<input v-model="form.display_name" name="display_name" required maxlength="128"
        /></label>
      </template>
      <template v-else-if="mode === 'reset'">
        <h2>重置密码 · {{ target?.username }}</h2>
        <label
          >新临时密码<input
            v-model="form.password"
            name="password"
            type="password"
            minlength="12"
            required
            autocomplete="new-password"
        /></label>
        <p class="safety-note">重置后该账号所有会话立即失效，下次登录必须修改密码。</p>
      </template>
      <template v-else-if="mode === 'status' && target">
        <h2>{{ target.status === 'ACTIVE' ? '停用账号' : '启用账号' }} · {{ target.username }}</h2>
        <p class="safety-note">
          {{
            target.status === 'ACTIVE'
              ? '停用后该账号无法登录，已登录的页面、实时连接和视频票据立即失效；任务、告警和审计记录保留。'
              : '启用后该账号可重新登录。'
          }}
        </p>
        <label
          >原因<input v-model="form.reason" name="reason" maxlength="500" placeholder="选填，写入审计"
        /></label>
      </template>
      <fieldset v-if="mode === 'create' || mode === 'roles'" class="weekday-picker" data-testid="role-picker">
        <legend>{{ mode === 'roles' ? `分配角色 · ${target?.username}` : '角色' }}</legend>
        <label v-for="code in grantableRoles" :key="code"
          ><input
            type="checkbox"
            :value="code"
            :checked="form.role_codes.includes(code)"
            :disabled="mode === 'create' && !canAssignRoles && code !== 'viewer'"
            @change="toggleRole(code)"
          />{{ roleLabel(code) }}</label
        >
      </fieldset>
      <p v-if="mode === 'roles'" class="form-hint">角色变更后，该账号已登录的会话会立即失效。</p>
      <div class="drawer-actions">
        <button type="button" class="secondary-button compact" @click="close">取消</button>
        <button class="primary-button compact" type="submit" :disabled="busy" data-testid="user-submit">
          {{
            busy
              ? '提交中…'
              : mode === 'status'
                ? target?.status === 'ACTIVE'
                  ? '确认停用'
                  : '确认启用'
                : '保存'
          }}
        </button>
      </div>
    </form>
  </div>
</template>

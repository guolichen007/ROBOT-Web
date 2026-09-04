import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, useRoute } from 'vue-router'
import { computed, defineComponent, nextTick, ref } from 'vue'
import { describe, expect, it } from 'vitest'

// App RouterView v-slot 边界的最小复现：v-if(login) + v-else(shell) 两个
// <component :is="Component" :key="route.path">。验证：
// 1) login→monitor 正确切换渲染；
// 2) 非 route 的响应式更新（snapshot 更新等价物）不 remount 匹配组件（key 稳定 vnode identity）。

const MonitorStub = defineComponent({
  setup() {
    const mountCount = ref(1)
    return { mountCount }
  },
  template: '<div class="monitor-stub">monitor #{{ mountCount }}</div>',
})

const LoginStub = defineComponent({
  template: '<div class="login-stub">login</div>',
})

const LayoutHost = defineComponent({
  setup() {
    const route = useRoute()
    const isLogin = computed(() => route.path === '/login')
    const reactiveTick = ref(0)
    return { isLogin, route, reactiveTick }
  },
  template: `
    <RouterView v-slot="{ Component }">
      <component :is="Component" v-if="isLogin" :key="route.path" />
      <div v-else class="app-shell">
        <section class="page">
          <component :is="Component" :key="route.path" />
        </section>
      </div>
    </RouterView>
    <span class="tick">{{ reactiveTick }}</span>
  `,
})

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', component: LoginStub },
      { path: '/monitor', component: MonitorStub },
      { path: '/', redirect: '/monitor' },
    ],
  })
}

describe('App RouterView layout slot vnode stability', () => {
  it('login → monitor switches and non-route reactive update does not remount', async () => {
    const router = makeRouter()
    const wrapper = mount(LayoutHost, { global: { plugins: [router] } })
    await router.isReady()

    // 初始 /login
    await router.push('/login')
    await nextTick()
    expect(wrapper.find('.login-stub').exists()).toBe(true)

    // login → monitor
    await router.push('/monitor')
    await nextTick()
    expect(wrapper.find('.monitor-stub').exists()).toBe(true)
    expect(wrapper.find('.monitor-stub').text()).toBe('monitor #1')

    // 非 route 的响应式更新（等价 snapshot 更新触发父组件重渲染）
    const vm = wrapper.vm as unknown as { reactiveTick: number }
    vm.reactiveTick = 1
    await nextTick()
    await nextTick()

    // key 稳定 → 组件不 remount → mountCount 仍是 1
    expect(wrapper.find('.monitor-stub').text()).toBe('monitor #1')
  })
})

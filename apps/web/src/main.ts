import { createPinia } from 'pinia'
import { createApp } from 'vue'
import { Alert, Button, Drawer, Empty, Skeleton, TabPanel, Tabs, Tag, Tooltip } from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import App from './App.vue'
import { router } from './router'
import './styles/main.css'

const app = createApp(App)
app.use(createPinia()).use(router)
for (const [name, component] of Object.entries({
  TAlert: Alert,
  TButton: Button,
  TDrawer: Drawer,
  TEmpty: Empty,
  TSkeleton: Skeleton,
  TTabPanel: TabPanel,
  TTabs: Tabs,
  TTag: Tag,
  TTooltip: Tooltip,
}))
  app.component(name, component)
app.mount('#app')

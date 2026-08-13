import axios from 'axios'
import { newUuid } from '@/lib/id'

let accessToken = ''
let refreshPromise: Promise<string> | null = null

function cookie(name: string): string {
  const match = document.cookie.split('; ').find((item) => item.startsWith(`${name}=`))
  return match ? decodeURIComponent(match.split('=').slice(1).join('=')) : ''
}

export const api = axios.create({ baseURL: '/api/v1', withCredentials: true, timeout: 12_000 })

export function setAccessToken(token: string): void {
  accessToken = token
}

export function clearAccessToken(): void {
  accessToken = ''
}

export function keepaliveRequest(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers)
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const csrf = cookie('csrf_token')
  if (csrf) headers.set('X-CSRF-Token', csrf)
  headers.set('X-Request-ID', newUuid())
  return fetch(`/api/v1${path}`, {
    ...init,
    headers,
    credentials: 'include',
    keepalive: true,
  })
}

export async function refreshAccessToken(): Promise<string> {
  if (!refreshPromise) {
    refreshPromise = axios
      .post(
        '/api/v1/auth/refresh',
        {},
        {
          withCredentials: true,
          headers: { 'X-CSRF-Token': cookie('csrf_token') },
        },
      )
      .then(({ data }) => {
        setAccessToken(data.access_token)
        return data.access_token as string
      })
      .finally(() => {
        refreshPromise = null
      })
  }
  return refreshPromise
}

api.interceptors.request.use((config) => {
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`
  const method = config.method?.toUpperCase()
  if (method && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = cookie('csrf_token')
    if (csrf) config.headers['X-CSRF-Token'] = csrf
  }
  config.headers['X-Request-ID'] = newUuid()
  return config
})

api.interceptors.response.use(undefined, async (error) => {
  const request = error.config
  if (error.response?.status !== 401 || request?._retried || request?.url?.includes('/auth/')) {
    throw error
  }
  request._retried = true
  const token = await refreshAccessToken()
  request.headers.Authorization = `Bearer ${token}`
  return api(request)
})

export function errorMessage(error: any): string {
  const platform = error?.response?.data?.error
  if (platform?.message) return `${platform.message}${platform.code ? ` (${platform.code})` : ''}`
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return error?.message || '请求失败'
}

export async function downloadAuthenticated(path: string, filename: string): Promise<void> {
  const response = await api.get(path, { responseType: 'blob' })
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

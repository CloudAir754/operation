import type { Overview, ResourceSnapshot, UserAccount } from './types'

export function readSessionToken(): string {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  const token = fragment.get('token') ?? sessionStorage.getItem('labopspanel-token') ?? ''
  if (token) {
    sessionStorage.setItem('labopspanel-token', token)
    history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
  }
  return token
}

export async function loadOverview(token: string): Promise<Overview> {
  const response = await fetch('/api/v1/overview', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) {
    throw new Error(response.status === 403 ? '启动会话令牌无效，请从终端重新打开页面。' : '读取本机状态失败。')
  }
  return response.json() as Promise<Overview>
}

export async function loadUsers(token: string): Promise<UserAccount[]> {
  const response = await fetch('/api/v1/users', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('读取本机账号失败。')
  return response.json() as Promise<UserAccount[]>
}

export async function loadResources(token: string): Promise<ResourceSnapshot> {
  const response = await fetch('/api/v1/resources', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('刷新 CPU 和内存状态失败。')
  return response.json() as Promise<ResourceSnapshot>
}

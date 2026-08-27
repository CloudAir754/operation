import type { GpuSnapshot, HomeScanTask, Overview, ResourceSnapshot, StorageSnapshot, UserAccount } from './types'

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

export async function loadStorage(token: string): Promise<StorageSnapshot> {
  const response = await fetch('/api/v1/storage', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('读取块设备状态失败。')
  return response.json() as Promise<StorageSnapshot>
}

export async function loadGpu(token: string): Promise<GpuSnapshot> {
  const response = await fetch('/api/v1/gpu', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('刷新 GPU 状态失败。')
  return response.json() as Promise<GpuSnapshot>
}

export async function startHomeScan(token: string, username: string): Promise<HomeScanTask> {
  const response = await fetch('/api/v1/home-scans', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({
      username,
      idempotency_key: crypto.randomUUID(),
      max_depth: 5,
      max_nodes: 20000,
    }),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(detail?.detail ?? '启动 home 扫描失败。')
  }
  return response.json() as Promise<HomeScanTask>
}

export async function cancelHomeScan(token: string, taskId: string): Promise<HomeScanTask> {
  const response = await fetch(`/api/v1/home-scans/${taskId}/cancel`, {
    method: 'POST',
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('取消扫描失败。')
  return response.json() as Promise<HomeScanTask>
}

export async function watchHomeScan(
  token: string,
  taskId: string,
  onUpdate: (task: HomeScanTask) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/v1/home-scans/${taskId}/events`, {
    headers: { Accept: 'text/event-stream', 'X-LabOps-Token': token },
    signal,
  })
  if (!response.ok || !response.body) throw new Error('连接扫描进度流失败。')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const events = buffer.split('\n\n')
    buffer = events.pop() ?? ''
    for (const event of events) {
      const data = event.split('\n').find(line => line.startsWith('data: '))
      if (data) onUpdate(JSON.parse(data.slice(6)) as HomeScanTask)
    }
    if (done) break
  }
}

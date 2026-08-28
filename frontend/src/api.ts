import type { GpuSnapshot, HomeScanTask, Overview, PamPolicyChangePreview, PamPolicyChangeValues, PamPolicySnapshot, PasswordPolicySummary, PasswordPolicyValues, PersistentTask, PortUsageSnapshot, PrivilegedCapability, ResourceSnapshot, StorageSnapshot, UserAccount, UserImportPreview } from './types'

async function errorDetail(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => null) as { detail?: string } | null
  return body?.detail ?? fallback
}

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

export async function previewTextImport(token: string, text: string): Promise<UserImportPreview> {
  const response = await fetch('/api/v1/user-imports/preview/text', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ text }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '文本预览失败。'))
  return response.json() as Promise<UserImportPreview>
}

export async function previewFileImport(
  token: string,
  kind: 'csv' | 'xlsx',
  file: File,
): Promise<UserImportPreview> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(`/api/v1/user-imports/preview/${kind}`, {
    method: 'POST',
    headers: { 'X-LabOps-Token': token },
    body: form,
  })
  if (!response.ok) throw new Error(await errorDetail(response, '文件预览失败。'))
  return response.json() as Promise<UserImportPreview>
}

export async function loadDefaultPasswordPolicy(token: string): Promise<PasswordPolicySummary> {
  const response = await fetch('/api/v1/password-policy/default', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('读取默认密码规则失败。')
  return response.json() as Promise<PasswordPolicySummary>
}

export async function previewPasswordPolicy(
  token: string,
  policy: PasswordPolicyValues,
): Promise<PasswordPolicySummary> {
  const response = await fetch('/api/v1/password-policy/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify(policy),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '密码规则无效。'))
  return response.json() as Promise<PasswordPolicySummary>
}

export async function loadPamPolicy(token: string): Promise<PamPolicySnapshot> {
  const response = await fetch('/api/v1/pam-policy', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('读取 PAM 策略失败。')
  return response.json() as Promise<PamPolicySnapshot>
}

export async function loadPrivilegedCapability(token: string): Promise<PrivilegedCapability> {
  const response = await fetch('/api/v1/privileged-capability', {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('读取特权助手能力失败。')
  return response.json() as Promise<PrivilegedCapability>
}

export async function createUsers(
  token: string,
  rows: Array<{ username: string, display_name: string }>,
  policy: PasswordPolicyValues,
): Promise<PersistentTask> {
  const response = await fetch('/api/v1/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ rows, ...policy, idempotency_key: crypto.randomUUID() }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '创建用户任务启动失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function loadTask(token: string, taskId: string): Promise<PersistentTask> {
  const response = await fetch(`/api/v1/tasks/${taskId}`, {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error('读取任务状态失败。')
  return response.json() as Promise<PersistentTask>
}

export async function downloadCredentials(token: string, taskId: string): Promise<void> {
  const response = await fetch(`/api/v1/tasks/${taskId}/credentials`, {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error(await errorDetail(response, '下载初始凭据失败。'))
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = `initial-credentials-${taskId}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

export async function deleteCredentials(token: string, taskId: string): Promise<void> {
  const response = await fetch(`/api/v1/tasks/${taskId}/credentials`, {
    method: 'DELETE',
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error(await errorDetail(response, '删除初始凭据失败。'))
}

export async function importSshKeys(
  token: string,
  username: string,
  keys: string,
): Promise<PersistentTask> {
  const response = await fetch(`/api/v1/users/${encodeURIComponent(username)}/ssh-keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ keys, idempotency_key: crypto.randomUUID() }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, 'SSH 公钥导入失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function changeSudo(
  token: string,
  username: string,
  grant: boolean,
): Promise<PersistentTask> {
  const action = grant ? 'grant' : 'revoke'
  const response = await fetch(`/api/v1/users/${encodeURIComponent(username)}/sudo/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, grant ? 'sudo 授权失败。' : 'sudo 撤销失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function previewPamChange(
  token: string,
  changes: PamPolicyChangeValues,
): Promise<PamPolicyChangePreview> {
  const response = await fetch('/api/v1/pam-policy/change-preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ changes }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, 'PAM 修改预览失败。'))
  return response.json() as Promise<PamPolicyChangePreview>
}

export async function applyPamChange(
  token: string,
  changes: PamPolicyChangeValues,
  confirmation: string,
): Promise<PersistentTask> {
  const response = await fetch('/api/v1/pam-policy/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ changes, confirmation, idempotency_key: crypto.randomUUID() }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, 'PAM 修改任务启动失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function restorePamBackup(
  token: string,
  backupId: string,
  confirmation: string,
): Promise<PersistentTask> {
  const response = await fetch('/api/v1/pam-policy/restore', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ backup_id: backupId, confirmation, idempotency_key: crypto.randomUUID() }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, 'PAM 恢复任务启动失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function lookupPort(
  token: string,
  port: number,
  protocol: 'tcp' | 'udp' | 'any',
): Promise<PortUsageSnapshot> {
  const response = await fetch(`/api/v1/ports/${port}?protocol=${protocol}`, {
    headers: { 'X-LabOps-Token': token },
  })
  if (!response.ok) throw new Error(await errorDetail(response, '查询端口占用失败。'))
  return response.json() as Promise<PortUsageSnapshot>
}

export async function terminateProcess(
  token: string,
  process: { pid: number, uid: number, create_time: number },
  confirmation: string,
): Promise<PersistentTask> {
  const response = await fetch(`/api/v1/processes/${process.pid}/terminate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({
      expected_uid: process.uid,
      expected_create_time: process.create_time,
      grace_seconds: 5,
      confirmation,
      idempotency_key: crypto.randomUUID(),
    }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '终止进程任务启动失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function terminateUserProcesses(
  token: string,
  username: string,
  confirmation: string,
): Promise<PersistentTask> {
  const response = await fetch(`/api/v1/users/${encodeURIComponent(username)}/processes/terminate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({
      grace_seconds: 5,
      confirmation,
      idempotency_key: crypto.randomUUID(),
    }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '结束用户全部进程任务启动失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function startUserDeletionPreflight(
  token: string,
  username: string,
): Promise<PersistentTask> {
  const response = await fetch(`/api/v1/users/${encodeURIComponent(username)}/deletion-preflight`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '用户删除预检启动失败。'))
  return response.json() as Promise<PersistentTask>
}

export async function deleteUser(
  token: string,
  username: string,
  preflightTaskId: string,
  deleteHome: boolean,
  confirmation: string,
): Promise<PersistentTask> {
  const response = await fetch(`/api/v1/users/${encodeURIComponent(username)}/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LabOps-Token': token },
    body: JSON.stringify({
      preflight_task_id: preflightTaskId,
      delete_home: deleteHome,
      confirmation,
      idempotency_key: crypto.randomUUID(),
    }),
  })
  if (!response.ok) throw new Error(await errorDetail(response, '删除用户任务启动失败。'))
  return response.json() as Promise<PersistentTask>
}

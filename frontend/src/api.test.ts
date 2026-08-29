import { afterEach, describe, expect, it, vi } from 'vitest'
import { previewSshKeys, readSessionToken } from './api'

describe('readSessionToken', () => {
  afterEach(() => {
    sessionStorage.clear()
    history.replaceState(null, '', '/')
    vi.restoreAllMocks()
  })

  it('从地址片段读取令牌并立即清理地址', () => {
    history.replaceState(null, '', '/#token=temporary-token')
    expect(readSessionToken()).toBe('temporary-token')
    expect(window.location.hash).toBe('')
    expect(sessionStorage.getItem('labopspanel-token')).toBe('temporary-token')
  })

  it('没有令牌时返回空字符串', () => {
    expect(readSessionToken()).toBe('')
  })

  it('公钥预览只向受令牌保护的校验端点发送内容', async () => {
    const payload = { counts: { valid: 1, duplicate: 0, invalid: 0 }, rows: [{ source_row: 1, status: 'valid', error: null, key_type: 'ssh-ed25519', comment: null, fingerprint: 'SHA256:test', duplicate: false }] }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }))

    await expect(previewSshKeys('session-token', 'ssh-ed25519 AAAA')).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/ssh-keys/preview', expect.objectContaining({ method: 'POST', headers: expect.objectContaining({ 'X-LabOps-Token': 'session-token' }) }))
  })
})

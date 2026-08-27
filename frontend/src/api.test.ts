import { afterEach, describe, expect, it } from 'vitest'
import { readSessionToken } from './api'

describe('readSessionToken', () => {
  afterEach(() => {
    sessionStorage.clear()
    history.replaceState(null, '', '/')
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
})

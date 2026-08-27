import { describe, expect, it } from 'vitest'
import type { UserAccount } from './types'
import { filterUsersByUsername } from './userFilters'

const users: UserAccount[] = [
  { username: 'alice', uid: 1000, gid: 1000, display_name: '', home: '/home/alice', resolved_home: '/home/alice', shell: '/bin/bash', account_source: 'local', account_status: 'N/A', home_storage: null, sudo_sources: [], sudo_sources_complete: true },
  { username: 'lab-admin', uid: 1001, gid: 1001, display_name: '', home: '/home/lab-admin', resolved_home: '/home/lab-admin', shell: '/bin/bash', account_source: 'local', account_status: 'N/A', home_storage: null, sudo_sources: ['sudo 组'], sudo_sources_complete: true },
]

describe('filterUsersByUsername', () => {
  it('忽略大小写并支持部分用户名', () => {
    expect(filterUsersByUsername(users, 'ADM')).toEqual([users[1]])
  })

  it('空搜索返回全部账号', () => {
    expect(filterUsersByUsername(users, '  ')).toEqual(users)
  })
})

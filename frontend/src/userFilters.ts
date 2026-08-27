import type { UserAccount } from './types'

export function filterUsersByUsername(users: UserAccount[], query: string): UserAccount[] {
  const normalizedQuery = query.trim().toLocaleLowerCase()
  if (!normalizedQuery) return users
  return users.filter(user => user.username.toLocaleLowerCase().includes(normalizedQuery))
}

"use client"

import { routerApi } from "@/lib/router-api"
import {
  routerGroupsUrl,
  routerGroupMembersUrl,
  routerUsersUrl,
} from "@/lib/urls"

// Groups-resource types + fetch helpers (Wave 5 extraction — the groups
// twin of tasks/tasks-api.ts + messages/messages-api.ts). The page + all
// of its satellites (columns, modals, detail panel, capabilities section)
// share these so the row shapes and the router GETs can never drift
// between surfaces.

export interface GroupRow {
  group_id: string
  name: string
  is_sysadmin: boolean
  created_at: string
  member_count: number
}

export interface MemberRow {
  user_id?: string
  username?: string
  group_id?: string
  name?: string
  member_group_is_sysadmin?: boolean
  added_at: string
}

export interface UserRow {
  user_id: string
  username: string
  email: string | null
  is_sysadmin: boolean
}

export async function fetchGroups(signal?: AbortSignal): Promise<GroupRow[]> {
  const body = await routerApi.request<{ groups?: GroupRow[] }>(
    routerGroupsUrl(),
    signal ? { signal } : {},
  )
  return body.groups || []
}

export async function fetchMembers(groupId: string): Promise<MemberRow[]> {
  const body = await routerApi.request<{ members?: MemberRow[] }>(
    routerGroupMembersUrl(groupId),
  )
  return body.members || []
}

export async function fetchUsers(): Promise<UserRow[]> {
  const body = await routerApi.request<{ users?: UserRow[] }>(
    routerUsersUrl(),
  )
  return body.users || []
}

export const memberLabel = (n: number): string =>
  `${n} member${n === 1 ? "" : "s"}`

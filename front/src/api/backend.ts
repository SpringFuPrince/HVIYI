import type { ChatHistoryMessages, ChatHistoryPage, LocalUser, Meeting, MeetingPage, OfficeTask, TaskPage } from '../types'

export interface ImportTaskStatus { code: number; task_id: string; status: 'pending' | 'processing' | 'completed' | 'failed' | ''; done_list: string[]; running_list: string[] }
export interface QueryTask { message: string; task_id: string; session_id: string; stream_url: string }
export interface QueryProgress { status: string; done_list: string[]; running_list: string[] }
export interface QueryDelta { message_id: string; delta: string }
export interface QueryFinal { message_id: string; answer: string; status: string; memory_warnings?: string[] }
export interface RecordingSession { session_id: string; meeting_id: string; status: string; started_at: string; last_sequence: number; websocket_url: string }
export interface TranscriptSegment { segment_id: string; sequence: number; speaker_name: string; text: string; start_ms: number; end_ms: number; confidence: number | null }
export interface RecordingEnd { session_id: string; meeting_id: string; status: string; transcript_generated: boolean; transcript_md_path: string | null; import_task_id: string | null; import_status: string }

const trimTrailingSlash = (value: string) => value.replace(/\/$/, '')
export const backendConfig = {
  importBaseUrl: trimTrailingSlash(import.meta.env.VITE_IMPORT_API_BASE_URL || '/api/import'),
  queryBaseUrl: trimTrailingSlash(import.meta.env.VITE_QUERY_API_BASE_URL || '/api/query'),
  localBaseUrl: trimTrailingSlash(import.meta.env.VITE_LOCAL_API_BASE_URL || '/api/local'),
}

export function setActiveMeeting(meeting: Meeting) { window.localStorage.setItem('hive_meeting_scope', JSON.stringify({ meetingId: meeting.meeting_id, title: meeting.title })) }
export function getActiveMeetingScope() {
  try {
    const value = JSON.parse(window.localStorage.getItem('hive_meeting_scope') || '{}') as { meetingId?: string; title?: string }
    return value.meetingId ? { meetingId: value.meetingId, title: value.title || '当前会议' } : null
  } catch { return null }
}

function apiUrl(baseUrl: string, path: string) { return /^https?:\/\//i.test(path) ? path : `${baseUrl}${path.startsWith('/') ? path : `/${path}`}` }
class BackendHttpError extends Error { constructor(message: string, readonly status: number) { super(message); this.name = 'BackendHttpError' } }

async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text()
  let body: Record<string, unknown> | null = null
  try { body = text ? JSON.parse(text) as Record<string, unknown> : null } catch { body = null }
  if (response.ok) return body as T
  throw new BackendHttpError(typeof body?.detail === 'string' ? body.detail : `请求失败（HTTP ${response.status}）`, response.status)
}

async function apiFetch<T>(baseUrl: string, path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  return readJson<T>(await fetch(apiUrl(baseUrl, path), { ...init, headers }))
}

const localFetch = <T>(path: string, init: RequestInit = {}) => apiFetch<T>(backendConfig.localBaseUrl, path, init)
export const getLocalProfile = () => localFetch<LocalUser>('/profile')
export const getNearestMeeting = () => localFetch<Meeting | null>('/meetings/nearest')
export function getMeetings(params: { page?: number; pageSize?: number; query?: string } = {}) {
  const search = new URLSearchParams({ page: String(params.page || 1), page_size: String(params.pageSize || 10) })
  if (params.query?.trim()) search.set('query', params.query.trim())
  return localFetch<MeetingPage>(`/meetings?${search}`)
}
export async function getAllMeetings() { return (await getMeetings({ page: 1, pageSize: 100 })).items }
export function createMeeting(payload: { title: string; description?: string; scheduled_start: string; scheduled_end?: string; location?: string; participant_count?: number }) { return localFetch<Meeting>('/meetings', { method: 'POST', body: JSON.stringify(payload) }) }
export function updateMeetingStatus(meetingId: string, status: Meeting['status']) { return localFetch<Meeting>(`/meetings/${encodeURIComponent(meetingId)}/status`, { method: 'PATCH', body: JSON.stringify({ status }) }) }
export function getMeetingDocuments(meetingId: string) { return localFetch<{ items: Array<{ document_id: string; meeting_id: string; file_name: string; source_type: string; status: string; error: string | null }> }>(`/meetings/${encodeURIComponent(meetingId)}/documents`) }
export function getTasks(params: { meetingId?: string; page?: number; pageSize?: number } = {}) {
  const search = new URLSearchParams({ page: String(params.page || 1), page_size: String(params.pageSize || 6), incomplete_only: 'true' })
  if (params.meetingId) search.set('meeting_id', params.meetingId)
  return localFetch<TaskPage>(`/tasks?${search}`)
}
export function updateTaskProgress(taskId: string, progress: number) { return localFetch<OfficeTask>(`/tasks/${encodeURIComponent(taskId)}/progress`, { method: 'PATCH', body: JSON.stringify({ progress }) }) }

export async function uploadDocuments(files: File[], meetingId: string) {
  const body = new FormData()
  files.forEach((file) => body.append('files', file, file.name))
  body.append('meeting_id', meetingId)
  return apiFetch<{ code: number; message: string; task_ids: string[] }>(backendConfig.importBaseUrl, '/upload', { method: 'POST', body })
}
export const getImportTaskStatus = (taskId: string) => apiFetch<ImportTaskStatus>(backendConfig.importBaseUrl, `/status/${encodeURIComponent(taskId)}`)
export const createQueryTask = (query: string, sessionId: string | null, meetingId: string) => apiFetch<QueryTask>(backendConfig.queryBaseUrl, '/query', { method: 'POST', body: JSON.stringify({ query, meeting_id: meetingId, session_id: sessionId, is_stream: true }) })
export function getQueryHistory(params: { meetingId: string; page?: number; pageSize?: number; search?: string }) {
  const search = new URLSearchParams({ meeting_id: params.meetingId, page: String(params.page || 1), page_size: String(params.pageSize || 20) })
  if (params.search?.trim()) search.set('search', params.search.trim())
  return apiFetch<ChatHistoryPage>(backendConfig.queryBaseUrl, `/history?${search}`)
}
export const getQueryHistoryMessages = (sessionId: string, meetingId: string) => apiFetch<ChatHistoryMessages>(backendConfig.queryBaseUrl, `/history/${encodeURIComponent(sessionId)}?meeting_id=${encodeURIComponent(meetingId)}`)

export const startMeetingRecording = (meetingId: string, mimeType: string) => localFetch<RecordingSession>(`/meetings/${encodeURIComponent(meetingId)}/recordings/start`, { method: 'POST', body: JSON.stringify({ mime_type: mimeType, language: 'zh-CN' }) })
export const getRecordingSegments = (sessionId: string) => localFetch<TranscriptSegment[]>(`/recordings/${encodeURIComponent(sessionId)}/segments`)
export const endMeetingRecording = (sessionId: string) => localFetch<RecordingEnd>(`/recordings/${encodeURIComponent(sessionId)}/end`, { method: 'POST' })
export const getRecordingImportStatus = (sessionId: string) => localFetch<{ task_id: string | null; status: string }>(`/recordings/${encodeURIComponent(sessionId)}/import-status`)
export function createRecordingSocket(path: string) {
  if (/^wss?:\/\//i.test(path)) return new WebSocket(path)
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return new WebSocket(`${scheme}//${window.location.host}${backendConfig.localBaseUrl}${path}`)
}
export const createQueryEventSource = (streamUrl: string) => new EventSource(apiUrl(backendConfig.queryBaseUrl, streamUrl))
export function parseSseData<T>(event: Event): T | null { try { return event instanceof MessageEvent ? JSON.parse(event.data) as T : null } catch { return null } }

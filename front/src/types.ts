export type View = 'home' | 'live' | 'meetings' | 'rag'

export interface LocalUser { display_name: string }

export interface Meeting {
  meeting_id: string
  title: string
  description: string | null
  status: 'scheduled' | 'in_progress' | 'completed' | 'cancelled'
  scheduled_start: string
  scheduled_end: string | null
  actual_start: string | null
  actual_end: string | null
  location: string | null
  participant_count: number
  created_at: string
  updated_at: string
}

export interface MeetingPage { items: Meeting[]; page: number; page_size: number; total: number }

export interface OfficeTask {
  task_id: string
  meeting_id: string
  title: string
  description: string | null
  owner: string | null
  status: string
  progress: number
  due_at: string | null
  created_at: string
  updated_at: string
}

export interface TaskPage { items: OfficeTask[]; page: number; page_size: number; total: number }

export interface ChatMessage { id: string; role: 'user' | 'assistant'; content: string; created_at?: string }
export interface TranscriptItem {
  id: string
  speaker: string
  initials: string
  color: 'blue' | 'amber' | 'green' | 'violet'
  timestamp: string
  content: string
}
export interface ChatHistoryItem { session_id: string; meeting_id: string; title: string; message_count: number; created_at: string; updated_at: string }
export interface ChatHistoryPage { items: ChatHistoryItem[]; page: number; page_size: number; total: number }
export interface ChatHistoryMessages {
  session_id: string
  meeting_id: string
  items: Array<{ sequence_id: number; message_id: string; turn_id: string; role: 'user' | 'assistant'; content: string; created_at: string }>
}

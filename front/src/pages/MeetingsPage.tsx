import { useEffect, useState } from 'react'
import {
  ArrowLeft, CalendarDays, CheckCircle2, Clock3, FileText, MessageSquareText,
  Play, Search, Sparkles, Users, X,
} from 'lucide-react'
import { getMeetings } from '../api/backend'
import type { LocalUser, Meeting, View } from '../types'

interface MeetingsPageProps {
  onNavigate: (view: View) => void
  onCreateMeeting: () => void
  onEnterMeeting: (meeting: Meeting) => void
  user: LocalUser
}

const dateTime = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })

const statusMap: Record<Meeting['status'], { label: string; className: string }> = {
  scheduled: { label: '即将开始', className: 'status-pill--scheduled' },
  in_progress: { label: '进行中', className: 'status-pill--processing' },
  completed: { label: '已完成', className: 'status-pill--ready' },
  cancelled: { label: '已取消', className: 'status-pill--cancelled' },
}

function duration(meeting: Meeting) {
  if (!meeting.scheduled_end) return '时长待定'
  return `${Math.max(1, Math.round((new Date(meeting.scheduled_end).getTime() - new Date(meeting.scheduled_start).getTime()) / 60_000))} 分钟`
}

export function MeetingsPage({ onNavigate, onCreateMeeting, onEnterMeeting, user }: MeetingsPageProps) {
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [page, setPage] = useState(1)
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<Meeting | null>(null)

  useEffect(() => {
    setLoading(true)
    setError('')
    getMeetings({ page, pageSize: 10, query: submittedQuery })
      .then((result) => { setMeetings(result.items); setTotal(result.total) })
      .catch((cause) => setError(cause instanceof Error ? cause.message : '会议列表加载失败'))
      .finally(() => setLoading(false))
  }, [page, submittedQuery])

  const search = (event: React.FormEvent) => {
    event.preventDefault()
    setPage(1)
    setSubmittedQuery(query.trim())
  }

  return (
    <div className="page meetings-page">
      <div className="page-heading">
        <div><span className="eyebrow"><CalendarDays size={14} /> MEETING LIBRARY</span><h1>会议记录</h1><p>浏览即将开始、进行中和已完成的本地会议。</p></div>
        <button className="primary-button" onClick={onCreateMeeting}><Play size={17} fill="currentColor" />发起新会议</button>
      </div>

      <section className="library-summary">
        <div><strong>{total}</strong><span>会议总数</span></div><i />
        <div><strong>{meetings.filter((meeting) => meeting.status === 'scheduled').length}</strong><span>本页即将开始</span></div><i />
        <div><strong>{meetings.filter((meeting) => meeting.status === 'in_progress').length}</strong><span>本页进行中</span></div><i />
        <div><strong>{meetings.filter((meeting) => meeting.status === 'completed').length}</strong><span>本页已完成</span></div>
        <div className="library-summary__art"><Sparkles size={18} /><span>{user.display_name}<br />本地会议空间</span></div>
      </section>

      <form className="library-toolbar" onSubmit={search}>
        <div className="library-search"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索会议标题或说明" /></div>
        <button type="submit"><Search size={16} />搜索</button>
      </form>
      {error && <p className="page-error" role="alert">{error}</p>}

      <section className="meeting-table-wrap">
        <table className="meeting-table">
          <thead><tr><th>会议</th><th>时间</th><th>参与者</th><th>地点</th><th>状态</th><th /></tr></thead>
          <tbody>{meetings.map((meeting, index) => {
            const status = statusMap[meeting.status]
            return <tr key={meeting.meeting_id} onClick={() => setSelected(meeting)}>
              <td><div className="meeting-cell"><span className={`meeting-cell__icon meeting-cell__icon--${(index % 3) + 1}`}><MessageSquareText size={18} /></span><div><strong>{meeting.title}</strong><span>{meeting.location && <em>{meeting.location}</em>}</span></div></div></td>
              <td><strong className="table-date">{dateTime.format(new Date(meeting.scheduled_start || meeting.created_at))}</strong><span className="table-sub">{duration(meeting)}</span></td>
              <td><div className="table-participants"><div className="mini-avatars"><i>{user.display_name.slice(0, 1)}</i></div><span>{meeting.participant_count} 人</span></div></td>
              <td><span className="table-sub">{meeting.location || '未设置'}</span></td>
              <td><span className={`status-pill ${status.className}`}>{meeting.status === 'completed' ? <CheckCircle2 size={14} /> : <i />}{status.label}</span></td>
              <td>{meeting.status !== 'completed' && meeting.status !== 'cancelled' && <button className="table-enter" onClick={(event) => { event.stopPropagation(); onEnterMeeting(meeting) }}><Play size={14} />进入</button>}</td>
            </tr>
          })}</tbody>
        </table>
        {loading && <div className="panel-loading"><span className="button-loader" />正在读取会议…</div>}
        {!loading && !meetings.length && <div className="empty-state"><Search size={25} /><strong>还没有会议记录</strong><span>创建会议后会显示在这里</span></div>}
      </section>
      <div className="table-footer"><span>显示 {meetings.length} / {total} 场会议</span><div><button disabled={page === 1} onClick={() => setPage((value) => value - 1)}>上一页</button><button className="active">{page}</button><button disabled={page * 10 >= total} onClick={() => setPage((value) => value + 1)}>下一页</button></div></div>

      {selected && <MeetingDetail meeting={selected} onClose={() => setSelected(null)} onAsk={() => { setSelected(null); onNavigate('rag') }} onEnter={() => onEnterMeeting(selected)} />}
    </div>
  )
}

function MeetingDetail({ meeting, onClose, onAsk, onEnter }: { meeting: Meeting; onClose: () => void; onAsk: () => void; onEnter: () => void }) {
  const status = statusMap[meeting.status]
  return <div className="detail-layer">
    <button className="detail-backdrop" onClick={onClose} aria-label="关闭详情" />
    <aside className="meeting-detail">
      <header><button className="icon-button" onClick={onClose}><ArrowLeft size={19} /></button><div><span>会议详情</span><strong>{meeting.title}</strong></div><button className="icon-button" onClick={onClose}><X size={19} /></button></header>
      <div className="meeting-detail__hero"><div className="detail-hero-top"><span className={`status-pill ${status.className}`}>{status.label}</span></div><h2>{meeting.title}</h2><div><span><CalendarDays size={15} />{dateTime.format(new Date(meeting.scheduled_start))}</span><span><Clock3 size={15} />{duration(meeting)}</span><span><Users size={15} />{meeting.participant_count} 位参与者</span></div></div>
      <div className="meeting-detail__body">
        <section className="summary-callout"><span><Sparkles size={18} /></span><div><strong>会议说明</strong><p>{meeting.description || '暂未填写会议说明。'}</p></div></section>
        <section className="detail-section"><h3>会议信息</h3><div className="meeting-domain-info"><p><strong>会议 ID</strong><span>{meeting.meeting_id}</span></p><p><strong>地点</strong><span>{meeting.location || '未设置'}</span></p></div></section>
      </div>
      <footer>{meeting.status === 'completed' ? <button className="ask-meeting-button" onClick={onAsk}><MessageSquareText size={17} />基于本次会议提问</button> : meeting.status !== 'cancelled' ? <button className="ask-meeting-button" onClick={onEnter}><Play size={17} />进入会议</button> : <span><FileText size={16} />该会议已取消</span>}</footer>
    </aside>
  </div>
}

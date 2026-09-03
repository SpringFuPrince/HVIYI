import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowRight, CalendarDays, CheckCircle2, Clock3, FileText, ListTodo, MessageSquareText, Play, Plus, Sparkles, Users } from 'lucide-react'
import { getNearestMeeting, getTasks, updateTaskProgress } from '../api/backend'
import type { LocalUser, Meeting, OfficeTask, View } from '../types'

const dateTime = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })
const timeOnly = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
const shortDate = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' })

function durationLabel(meeting: Meeting) {
  if (!meeting.scheduled_end) return '时长待定'
  const minutes = Math.max(1, Math.round((new Date(meeting.scheduled_end).getTime() - new Date(meeting.scheduled_start).getTime()) / 60_000))
  return `${minutes} 分钟`
}

function meetingStatus(meeting: Meeting) {
  if (meeting.status === 'in_progress') return '正在进行'
  if (meeting.status === 'completed') return '最近完成'
  return '即将开始'
}

function countdown(meeting: Meeting, now: number) {
  if (meeting.status === 'in_progress') return '会议正在进行中'
  if (meeting.status === 'completed') return `完成于 ${shortDate.format(new Date(meeting.actual_end || meeting.scheduled_start))}`
  const seconds = Math.max(0, Math.floor((new Date(meeting.scheduled_start).getTime() - now) / 1000))
  const hours = Math.floor(seconds / 3600).toString().padStart(2, '0')
  const minutes = Math.floor((seconds % 3600) / 60).toString().padStart(2, '0')
  const remain = (seconds % 60).toString().padStart(2, '0')
  return `距开始 ${hours}:${minutes}:${remain}`
}

export function HomePage({ user, onNavigate, onCreateMeeting, onEnterMeeting }: {
  user: LocalUser
  onNavigate: (view: View) => void
  onCreateMeeting: () => void
  onEnterMeeting: (meeting: Meeting) => void
}) {
  const [meeting, setMeeting] = useState<Meeting | null>(null)
  const [tasks, setTasks] = useState<OfficeTask[]>([])
  const [taskPage, setTaskPage] = useState(1)
  const [taskTotal, setTaskTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [now, setNow] = useState(Date.now())

  const loadTasks = useCallback(async (page: number) => {
    const result = await getTasks({ page, pageSize: 5 })
    setTasks(result.items)
    setTaskTotal(result.total)
  }, [])

  useEffect(() => {
    setLoading(true)
    setError('')
    Promise.all([getNearestMeeting(), loadTasks(taskPage)])
      .then(([nearest]) => setMeeting(nearest))
      .catch((cause) => setError(cause instanceof Error ? cause.message : '首页数据加载失败'))
      .finally(() => setLoading(false))
  }, [loadTasks, taskPage])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const setProgress = async (task: OfficeTask, progress: number) => {
    setTasks((current) => current.map((item) => item.task_id === task.task_id ? { ...item, progress } : item))
    try {
      await updateTaskProgress(task.task_id, progress)
      if (progress === 100) await loadTasks(taskPage)
    } catch (cause) {
      setTasks((current) => current.map((item) => item.task_id === task.task_id ? task : item))
      setError(cause instanceof Error ? cause.message : '任务进度更新失败')
    }
  }

  const today = new Date()
  const greeting = today.getHours() < 12 ? '上午好' : today.getHours() < 18 ? '下午好' : '晚上好'

  return (
    <div className="page page--home">
      <div className="page-heading home-heading">
        <div><span className="date-caption">{dateTime.format(today)}</span><h1>{greeting}，{user.display_name}</h1><p>从会议到行动，把组织里的每一项工作持续推进。</p></div>
        <button className="primary-button" onClick={onCreateMeeting}><Plus size={18} />发起新会议</button>
      </div>

      {meeting ? <section className="next-meeting-card">
        <div className="next-meeting-card__main">
          <span className="eyebrow"><CalendarDays size={14} /> {meetingStatus(meeting)} · {timeOnly.format(new Date(meeting.scheduled_start))}</span>
          <h2>{meeting.title}</h2>
          <p>{meeting.description || '记录讨论、沉淀任务，并可基于会议资料继续提问。'}</p>
          <div className="meeting-meta-row"><span><Clock3 size={15} />{durationLabel(meeting)}</span><span><Users size={15} />{meeting.participant_count} 位参与者</span></div>
        </div>
        <div className="next-meeting-card__side">
          <div className="time-block"><strong>{timeOnly.format(new Date(meeting.scheduled_start))}</strong><span>{countdown(meeting, now)}</span></div>
          {meeting.status === 'completed'
            ? <button onClick={() => onNavigate('meetings')}><FileText size={17} />查看记录</button>
            : <button onClick={() => onEnterMeeting(meeting)}><Play size={17} fill="currentColor" />进入会议</button>}
        </div>
        <div className="subtle-grid" />
      </section> : <section className="next-meeting-card next-meeting-card--empty">
        <div><span className="eyebrow"><CalendarDays size={14} /> 暂无会议</span><h2>安排团队的下一次讨论</h2><p>创建会议后，这里会展示最近一场即将开始、正在进行或已完成的会议。</p></div>
        <button onClick={onCreateMeeting}><Plus size={17} />创建会议</button>
      </section>}

      {error && <p className="page-error" role="alert">{error}</p>}

      <section className="panel task-panel">
        <div className="panel-header"><div><h3>我的待办事项</h3><p>按截止时间读取未完成任务，点击百分比即可更新进度</p></div><span className="task-total">{taskTotal} 项未完成</span></div>
        {loading ? <div className="panel-loading"><span className="button-loader" />正在读取待办…</div> : tasks.length ? <div className="todo-list">
          {tasks.map((task) => <article className="todo-item" key={task.task_id}>
            <div className="todo-item__icon"><ListTodo size={18} /></div>
            <div className="todo-item__body"><div><strong>{task.title}</strong><span>{task.due_at ? `${shortDate.format(new Date(task.due_at))} 截止` : '未设置截止时间'}</span></div><p>{task.description || task.owner || '来自会议行动项'}</p><div className="task-progress"><i><b style={{ width: `${task.progress}%` }} /></i><strong>{task.progress}%</strong></div></div>
            <div className="progress-actions" aria-label={`${task.title}进度`}>{[25, 50, 75, 100].map((progress) => <button key={progress} className={task.progress === progress ? 'active' : ''} onClick={() => void setProgress(task, progress)}>{progress}%</button>)}</div>
          </article>)}
        </div> : <div className="empty-tasks"><CheckCircle2 size={26} /><strong>当前没有未完成任务</strong><span>会议提取出的行动项会显示在这里。</span></div>}
        {taskTotal > 5 && <div className="task-pagination"><button disabled={taskPage === 1} onClick={() => setTaskPage((page) => page - 1)}>上一页</button><span>第 {taskPage} / {Math.ceil(taskTotal / 5)} 页</span><button disabled={taskPage >= Math.ceil(taskTotal / 5)} onClick={() => setTaskPage((page) => page + 1)}>下一页</button></div>}
      </section>

      <div className="home-grid home-grid--calendar">
        <CalendarPanel meeting={meeting} />
        <aside className="home-side">
          <section className="panel quick-panel">
            <div className="panel-header"><div><h3>快速开始</h3><p>继续你的会议工作</p></div></div>
            <button onClick={() => onNavigate('rag')}><span className="quick-icon quick-icon--blue"><MessageSquareText size={19} /></span><div><strong>询问会议内容</strong><small>跨会议检索与追问</small></div><ArrowRight size={16} /></button>
            <button onClick={() => onNavigate('meetings')}><span className="quick-icon quick-icon--sand"><FileText size={19} /></span><div><strong>浏览会议记录</strong><small>查看组织内全部会议</small></div><ArrowRight size={16} /></button>
          </section>
          <section className="ai-insight-card"><div className="ai-insight-card__top"><span><Sparkles size={17} /></span><strong>工作提示</strong></div><p>完成会议后，系统会将识别出的行动项写入待办列表，后续进度可持续更新。</p></section>
        </aside>
      </div>
    </div>
  )
}

function CalendarPanel({ meeting }: { meeting: Meeting | null }) {
  const today = new Date()
  const year = today.getFullYear()
  const month = today.getMonth()
  const firstWeekday = new Date(year, month, 1).getDay()
  const days = new Date(year, month + 1, 0).getDate()
  const cells = useMemo(() => [...Array(firstWeekday).fill(null), ...Array.from({ length: days }, (_, index) => index + 1)], [days, firstWeekday])
  const meetingDate = meeting ? new Date(meeting.scheduled_start) : null
  return <section className="panel calendar-panel">
    <div className="panel-header"><div><h3>{year}年{month + 1}月</h3><p>简约日历 · 今日与最近会议</p></div><CalendarDays size={20} /></div>
    <div className="calendar-weekdays">{['日', '一', '二', '三', '四', '五', '六'].map((day) => <span key={day}>{day}</span>)}</div>
    <div className="calendar-grid">{cells.map((day, index) => day === null ? <i key={`empty-${index}`} /> : <span key={day} className={`${day === today.getDate() ? 'today' : ''} ${meetingDate && meetingDate.getFullYear() === year && meetingDate.getMonth() === month && meetingDate.getDate() === day ? 'has-meeting' : ''}`}>{day}</span>)}</div>
    {meeting && <div className="calendar-agenda"><i /><div><strong>{timeOnly.format(new Date(meeting.scheduled_start || meeting.created_at))} · {meeting.title}</strong><span>{meeting.location || '本地会议'}</span></div></div>}
  </section>
}

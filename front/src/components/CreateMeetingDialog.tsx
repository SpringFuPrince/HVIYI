import { useState } from 'react'
import { CalendarClock, Clock3, MapPin, X } from 'lucide-react'
import { createMeeting } from '../api/backend'
import type { Meeting } from '../types'

function initialStart() {
  const date = new Date()
  date.setMinutes(0, 0, 0)
  date.setHours(date.getHours() + 1)
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}

export function CreateMeetingDialog({ onClose, onCreated }: {
  onClose: () => void
  onCreated: (meeting: Meeting) => void
}) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [start, setStart] = useState(initialStart)
  const [duration, setDuration] = useState(45)
  const [location, setLocation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError('')
    try {
      const startDate = new Date(start)
      const endDate = new Date(startDate.getTime() + duration * 60_000)
      const meeting = await createMeeting({
        title,
        description: description || undefined,
        scheduled_start: startDate.toISOString(),
        scheduled_end: endDate.toISOString(),
        location: location || undefined,
      })
      onCreated(meeting)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '会议创建失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="dialog-layer" role="dialog" aria-modal="true" aria-labelledby="create-meeting-title">
      <button className="detail-backdrop" onClick={onClose} aria-label="关闭创建会议窗口" />
      <form className="create-meeting-dialog" onSubmit={submit}>
        <header><div><span>NEW MEETING</span><h2 id="create-meeting-title">发起新会议</h2><p>会议、文档、任务和问答都以本次会议为唯一边界。</p></div><button type="button" className="icon-button" onClick={onClose}><X size={19} /></button></header>
        <div className="create-meeting-form">
          <label><span>会议名称</span><input value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={255} placeholder="输入会议主题" autoFocus /></label>
          <div className="form-grid-two">
            <label><span>开始时间</span><div className="input-wrap"><CalendarClock size={17} /><input type="datetime-local" value={start} onChange={(event) => setStart(event.target.value)} required /></div></label>
            <label><span>预计时长</span><div className="select-wrap"><Clock3 size={17} /><select value={duration} onChange={(event) => setDuration(Number(event.target.value))}><option value={30}>30 分钟</option><option value={45}>45 分钟</option><option value={60}>60 分钟</option><option value={90}>90 分钟</option></select></div></label>
          </div>
          <label><span>地点 / 会议链接</span><div className="input-wrap"><MapPin size={17} /><input value={location} onChange={(event) => setLocation(event.target.value)} maxLength={255} placeholder="选填" /></div></label>
          <label><span>会议说明</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={5000} rows={4} placeholder="选填：本次会议要讨论什么？" /></label>
          {error && <p className="form-error" role="alert">{error}</p>}
        </div>
        <footer><button type="button" onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={loading}>{loading ? '正在创建…' : '创建并进入会议'}</button></footer>
      </form>
    </div>
  )
}

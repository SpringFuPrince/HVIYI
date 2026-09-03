import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Bot, Check, ChevronDown, MessageSquare,
  MoreHorizontal, Network, Paperclip, Plus, Search, Send, Sparkles, ThumbsDown,
  ThumbsUp, UserRound, X,
} from 'lucide-react'
import type { ChatHistoryItem, ChatMessage, Meeting } from '../types'
import {
  createQueryEventSource, createQueryTask, getAllMeetings,
  getQueryHistory, getQueryHistoryMessages, parseSseData,
  type QueryDelta, type QueryFinal, type QueryProgress,
} from '../api/backend'

const initialMessages: ChatMessage[] = []

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '对话请求失败，请稍后重试。'
}

export function RagPage() {
  const [messages, setMessages] = useState(initialMessages)
  const [input, setInput] = useState('')
  const [thinking, setThinking] = useState(false)
  const [progressText, setProgressText] = useState('正在创建查询任务')
  const [requestError, setRequestError] = useState('')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [historyItems, setHistoryItems] = useState<ChatHistoryItem[]>([])
  const [historySearch, setHistorySearch] = useState('')
  const [historyLoading, setHistoryLoading] = useState(false)
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [historyRefresh, setHistoryRefresh] = useState(0)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [meetingsLoading, setMeetingsLoading] = useState(true)
  const [selectedMeetingId, setSelectedMeetingId] = useState<string | null>(null)
  const [draftMeetingId, setDraftMeetingId] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const streamRef = useRef<EventSource | null>(null)
  const historyRequestRef = useRef(0)
  const messageRequestRef = useRef(0)
  const autoRestoreRef = useRef(true)
  const previousMeetingRef = useRef<string | null | undefined>(undefined)
  const meetingId = selectedMeetingId
  const selectedMeeting = meetings.find((meeting) => meeting.meeting_id === meetingId) || null

  useEffect(() => () => {
    streamRef.current?.close()
    historyRequestRef.current += 1
    messageRequestRef.current += 1
  }, [])

  useEffect(() => {
    let cancelled = false
    setMeetingsLoading(true)
    getAllMeetings()
      .then((items) => {
        if (!cancelled) {
          setMeetings(items)
          setSelectedMeetingId((current) => current || items[0]?.meeting_id || null)
        }
      })
      .catch((error) => {
        if (!cancelled) setRequestError(errorMessage(error))
      })
      .finally(() => {
        if (!cancelled) setMeetingsLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    const target = endRef.current
    if (!target) return

    const frame = window.requestAnimationFrame(() => {
      if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ behavior: messages.length > initialMessages.length ? 'smooth' : 'auto', block: 'end' })
        return
      }

      const scrollContainer = target.closest('.chat-scroll')
      if (scrollContainer instanceof HTMLElement) scrollContainer.scrollTop = scrollContainer.scrollHeight
    })

    return () => window.cancelAnimationFrame(frame)
  }, [messages.length, thinking])

  const upsertAssistantMessage = (messageId: string, content: string, replace = false) => {
    setMessages((current) => {
      const index = current.findIndex((message) => message.id === messageId)
      if (index < 0) return [...current, { id: messageId, role: 'assistant', content }]
      return current.map((message, messageIndex) => messageIndex === index
        ? { ...message, content: replace ? content : `${message.content}${content}` }
        : message)
    })
  }

  const stopStream = useCallback(() => {
    streamRef.current?.close()
    streamRef.current = null
  }, [])

  const openHistory = useCallback(async (targetSessionId: string) => {
    if (!meetingId) return
    stopStream()
    autoRestoreRef.current = false
    const requestId = ++messageRequestRef.current
    setSessionId(targetSessionId)
    setMessages([])
    setMessagesLoading(true)
    setThinking(false)
    setRequestError('')
    try {
      const history = await getQueryHistoryMessages(targetSessionId, meetingId)
      if (requestId !== messageRequestRef.current) return
      setMessages(history.items.map((message) => ({
        id: message.message_id,
        role: message.role,
        content: message.content,
        created_at: message.created_at,
      })))
    } catch (error) {
      if (requestId === messageRequestRef.current) setRequestError(errorMessage(error))
    } finally {
      if (requestId === messageRequestRef.current) setMessagesLoading(false)
    }
  }, [meetingId, stopStream])

  useEffect(() => {
    if (!meetingId) {
      setHistoryItems([])
      setHistoryLoading(false)
      return
    }
    const meetingChanged = previousMeetingRef.current !== meetingId
    previousMeetingRef.current = meetingId
    if (meetingChanged) {
      stopStream()
      messageRequestRef.current += 1
      autoRestoreRef.current = true
      setSessionId(null)
      setMessages([])
      setThinking(false)
      setRequestError('')
    }

    const requestId = ++historyRequestRef.current
    setHistoryLoading(true)
    const timer = window.setTimeout(async () => {
      try {
        const page = await getQueryHistory({
          meetingId,
          page: 1,
          pageSize: 30,
          search: historySearch,
        })
        if (requestId !== historyRequestRef.current) return
        setHistoryItems(page.items)
        if (autoRestoreRef.current && !historySearch.trim()) {
          autoRestoreRef.current = false
          if (page.items[0]) void openHistory(page.items[0].session_id)
        }
      } catch (error) {
        if (requestId === historyRequestRef.current) setRequestError(errorMessage(error))
      } finally {
        if (requestId === historyRequestRef.current) setHistoryLoading(false)
      }
    }, historySearch ? 250 : 0)

    return () => window.clearTimeout(timer)
  }, [historyRefresh, historySearch, meetingId, openHistory, stopStream])

  const newChat = () => {
    stopStream()
    messageRequestRef.current += 1
    autoRestoreRef.current = false
    setMessages([])
    setSessionId(null)
    setMessagesLoading(false)
    setThinking(false)
    setRequestError('')
    setProgressText('正在创建查询任务')
  }

  const applyKnowledgeScope = () => {
    setScopeOpen(false)
    if (draftMeetingId === selectedMeetingId) return
    setSelectedMeetingId(draftMeetingId)
  }

  const send = async (text = input) => {
    const clean = text.trim()
    if (!clean || thinking) return
    if (!meetingId) {
      setRequestError('请先选择一场会议后再提问。')
      return
    }
    stopStream()
    setMessages((current) => [...current, { id: `u-${crypto.randomUUID()}`, role: 'user', content: clean }])
    setInput('')
    setRequestError('')
    setProgressText('正在创建查询任务')
    setThinking(true)

    try {
      const task = await createQueryTask(clean, sessionId, meetingId)
      setSessionId(task.session_id)
      autoRestoreRef.current = false
      setHistoryRefresh((current) => current + 1)
      const stream = createQueryEventSource(task.stream_url)
      streamRef.current = stream

      stream.addEventListener('ready', () => setProgressText('已连接，正在理解问题'))
      stream.addEventListener('progress', (event) => {
        const progress = parseSseData<QueryProgress>(event)
        if (!progress) return
        setProgressText(progress.running_list[0] || progress.done_list.at(-1) || '正在处理')
      })
      stream.addEventListener('delta', (event) => {
        const delta = parseSseData<QueryDelta>(event)
        if (!delta?.message_id || !delta.delta) return
        setThinking(false)
        upsertAssistantMessage(delta.message_id, delta.delta)
      })
      stream.addEventListener('final', (event) => {
        const final = parseSseData<QueryFinal>(event)
        if (final?.message_id) upsertAssistantMessage(final.message_id, final.answer || '', true)
        setThinking(false)
        stopStream()
        setHistoryRefresh((current) => current + 1)
      })
      stream.addEventListener('error', (event) => {
        const payload = parseSseData<{ message?: string }>(event)
        setRequestError(payload?.message || '流式连接已断开，请重新发送。')
        setThinking(false)
        stopStream()
        setHistoryRefresh((current) => current + 1)
      })
    } catch (error) {
      setRequestError(errorMessage(error))
      setThinking(false)
    }
  }

  return (
    <div className="rag-page">
      <aside className="chat-history-panel">
        <div className="chat-history__heading"><div><span><Bot size={18} /></span><strong>会议问答</strong></div><button className="icon-button"><X size={17} /></button></div>
        <button className="new-chat" onClick={newChat}><Plus size={16} />新建对话</button>
        <div className="history-search"><Search size={15} /><input value={historySearch} onChange={(event) => setHistorySearch(event.target.value)} placeholder="搜索历史对话" /></div>
        {historyLoading && !historyItems.length ? (
          <div className="empty-state"><MessageSquare size={22} /><strong>正在加载历史对话</strong><span>从 MySQL 读取当前知识范围的问答记录。</span></div>
        ) : historyItems.length ? (
          <div className="history-section"><span>{historySearch.trim() ? '搜索结果' : '最近对话'}</span>{historyItems.map((item) => (
            <button type="button" key={item.session_id} className={sessionId === item.session_id ? 'active' : ''} onClick={() => void openHistory(item.session_id)}>
              <MessageSquare size={14} /><p>{item.title}</p><small>{item.message_count}</small>
            </button>
          ))}</div>
        ) : (
          <div className="empty-state"><MessageSquare size={22} /><strong>{historySearch.trim() ? '没有匹配的历史对话' : '暂无历史对话'}</strong><span>发送问题后，对话会保存在当前知识范围中。</span></div>
        )}
        <div className="history-footer">历史消息按当前会议和会话隔离保存</div>
      </aside>

      <section className="chat-workspace">
        <header className="chat-header">
          <div><h1>{selectedMeeting?.title || '请选择会议'}</h1><span>每次问答只在一场会议范围内检索和记忆。</span></div>
          <div className="scope-picker-wrap">
            <button className="scope-picker" onClick={() => { setDraftMeetingId(selectedMeetingId); setScopeOpen(!scopeOpen) }}><span><Network size={15} /></span><div><small>知识范围</small><strong>{selectedMeeting?.title || '请选择会议'}</strong></div><ChevronDown size={15} /></button>
            {scopeOpen && <div className="scope-popover"><strong>选择会议知识范围</strong><div className="scope-options">{meetings.map((meeting) => <label key={meeting.meeting_id}><input type="radio" name="meeting-scope" checked={draftMeetingId === meeting.meeting_id} onChange={() => setDraftMeetingId(meeting.meeting_id)} /><span><Check size={12} /></span><div>{meeting.title}<small>{formatMeetingStatus(meeting.status)}</small></div></label>)}{meetingsLoading && <div className="scope-loading">正在加载会议列表…</div>}{!meetingsLoading && !meetings.length && <div className="scope-loading">暂无可选择的会议</div>}</div><button onClick={applyKnowledgeScope}>确定</button></div>}
          </div>
          <button className="icon-button"><MoreHorizontal size={19} /></button>
        </header>

        <div className="chat-scroll">
          <div className="chat-date"><span>{messages[0]?.created_at ? formatChatDate(messages[0].created_at) : '今天'}</span></div>
          {!messages.length && !thinking && <div className="chat-empty"><Sparkles size={22} /><strong>{messagesLoading ? '正在恢复历史消息' : '开始一次真实的会议问答'}</strong><span>{messagesLoading ? '消息将按数据库写入顺序显示。' : '问题会发送到查询服务，并实时显示后端返回的答案。'}</span></div>}
          {messages.map((message) => message.role === 'user' ? (
            <article className="chat-message chat-message--user" key={message.id}><div className="chat-avatar chat-avatar--user"><UserRound size={17} /></div><div><p>{message.content}</p></div></article>
          ) : (
            <article className="chat-message chat-message--assistant" key={message.id}>
              <div className="chat-avatar chat-avatar--ai"><Sparkles size={17} /></div>
              <div className="assistant-answer">
                <div className="answer-label"><strong>HVIYI AI</strong><span><Check size={12} />基于会议知识回答</span></div>
                <div className="answer-copy">{renderContent(message.content)}</div>
                <div className="answer-actions"><button><ThumbsUp size={15} /></button><button><ThumbsDown size={15} /></button><button>复制</button><button>保存为笔记</button></div>
              </div>
            </article>
          ))}
          {thinking && <article className="chat-message chat-message--assistant"><div className="chat-avatar chat-avatar--ai"><Sparkles size={17} /></div><div className="thinking-card"><div className="thinking-dots"><i /><i /><i /></div><div><strong>正在处理</strong><span>{progressText}</span></div></div></article>}
          <div ref={endRef} />
        </div>

        <footer className="chat-composer-wrap">
          {requestError && <div className="chat-error" role="alert"><span>{requestError}</span><button onClick={() => setRequestError('')}>关闭</button></div>}
          <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); send() }}><textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="询问任意会议内容，输入 / 选择知识范围" rows={1} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send() } }} /><div><button type="button" className="icon-button"><Paperclip size={18} /></button><span>AI 可能出错，请核对引用来源</span><button className="send-button" type="submit" disabled={!input.trim() || thinking}><Send size={17} /></button></div></form>
        </footer>
      </section>
    </div>
  )
}

function formatChatDate(value: string) {
  const date = new Date(value.endsWith('Z') ? value : `${value}Z`)
  if (Number.isNaN(date.getTime())) return '历史对话'
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return '今天'
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' }).format(date)
}

function formatMeetingStatus(status: Meeting['status']) {
  return {
    scheduled: '即将开始',
    in_progress: '进行中',
    completed: '已结束',
    cancelled: '已取消',
  }[status]
}

function renderContent(content: string) {
  return content.split('\n').filter(Boolean).map((line, index) => {
    const image = line.match(/^!\[([^\]]*)\]\((https?:\/\/[^)]+)\)$/)
    if (image) return <img className="answer-image" key={index} src={image[2]} alt={image[1] || '回答相关图片'} loading="lazy" />

    const numbered = line.match(/^(\d+)\.\s*(.*)$/)
    if (numbered) return <p className="numbered-answer" key={index}><span>{numbered[1]}</span><span>{renderInlineMarkdown(numbered[2])}</span></p>
    return <p key={index}>{renderInlineMarkdown(line)}</p>
  })
}

function renderInlineMarkdown(content: string) {
  return content.split(/(\*\*.*?\*\*)/g).filter(Boolean).map((part, index) =>
    part.startsWith('**') && part.endsWith('**')
      ? <strong key={index}>{part.slice(2, -2)}</strong>
      : part,
  )
}

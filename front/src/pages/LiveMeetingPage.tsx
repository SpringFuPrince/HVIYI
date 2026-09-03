import { useEffect, useRef, useState } from 'react'
import {
  ArrowLeft, CalendarDays, Check, CircleStop, Clock3, File, FileText, MapPin, Mic2,
  MoreHorizontal, Pause, Plus, Radio, Sparkles, UploadCloud, Users, X,
} from 'lucide-react'
import type { LocalUser, Meeting, TranscriptItem, View } from '../types'
import {
  createRecordingSocket, endMeetingRecording, getImportTaskStatus,
  getMeetingDocuments, getRecordingImportStatus, getRecordingSegments, startMeetingRecording,
  uploadDocuments, updateMeetingStatus, type TranscriptSegment,
} from '../api/backend'

const waveform = [9, 15, 22, 12, 27, 34, 18, 14, 29, 38, 24, 11, 17, 32, 41, 26, 14, 35, 28, 17, 24, 39, 31, 13, 19, 28, 37, 20, 12, 25, 34, 23]
const supportedExtensions = new Set(['pdf', 'pptx', 'docx', 'md'])
const maxFileSize = 100 * 1024 * 1024
const asrSampleRate = 16_000

interface LiveMeetingPageProps { onNavigate: (view: View) => void; meeting: Meeting | null; user: LocalUser }
type DocumentStatus = 'ready' | 'loading' | 'failed'

interface MeetingDocument {
  id: string
  name: string
  size: string
  detail: string
  status: DocumentStatus
  taskId?: string
}

function fileSizeLabel(size: number) {
  return `${Math.max(.1, size / 1024 / 1024).toFixed(1)} MB`
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '操作失败'
}

function timestamp(milliseconds: number) {
  const seconds = Math.floor(milliseconds / 1000)
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

const liveCaptionCharacterLimit = 180

export function limitLiveCaption(content: string, limit = liveCaptionCharacterLimit) {
  const characters = Array.from(content.trim())
  if (characters.length <= limit) return { text: characters.join(''), truncated: false }
  return { text: `…${characters.slice(-limit).join('')}`, truncated: true }
}

function encodePcm16(input: Float32Array, inputRate: number) {
  const outputLength = Math.max(1, Math.round(input.length * asrSampleRate / inputRate))
  const output = new Int16Array(outputLength)
  const ratio = inputRate / asrSampleRate
  for (let index = 0; index < outputLength; index += 1) {
    const start = Math.floor(index * ratio)
    const end = Math.max(start + 1, Math.min(input.length, Math.floor((index + 1) * ratio)))
    let total = 0
    for (let source = start; source < end; source += 1) total += input[source]
    const sample = Math.max(-1, Math.min(1, total / (end - start)))
    output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff
  }
  return output.buffer
}

export function LiveMeetingPage({ onNavigate, meeting, user }: LiveMeetingPageProps) {
  const [recording, setRecording] = useState(false)
  const [starting, setStarting] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [items, setItems] = useState<TranscriptItem[]>([])
  const [liveSegmentId, setLiveSegmentId] = useState<string | null>(null)
  const [docs, setDocs] = useState<MeetingDocument[]>([])
  const [lastSavedAt, setLastSavedAt] = useState('')
  const [showEnd, setShowEnd] = useState(false)
  const [processing, setProcessing] = useState(false)
  const [step, setStep] = useState(0)
  const [completionHasTranscript, setCompletionHasTranscript] = useState<boolean | null>(null)
  const [pageError, setPageError] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const pollTimers = useRef(new Set<number>())
  const streamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const sourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const workletNodeRef = useRef<AudioWorkletNode | null>(null)
  const silentGainRef = useRef<GainNode | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  const secondsRef = useRef(0)
  const recordingRef = useRef(false)
  const transcriptHistoryRef = useRef<HTMLDivElement>(null)

  const setRecordingState = (value: boolean) => {
    recordingRef.current = value
    setRecording(value)
  }

  useEffect(() => {
    if (!recording) return
    const timer = window.setInterval(() => {
      secondsRef.current += 1
      setSeconds(secondsRef.current)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [recording])

  const liveTranscript = liveSegmentId ? items.find((item) => item.id === liveSegmentId) : undefined
  const visibleLiveCaption = liveTranscript ? limitLiveCaption(liveTranscript.content) : null
  const transcriptHistory = liveSegmentId ? items.filter((item) => item.id !== liveSegmentId) : items

  useEffect(() => {
    const container = transcriptHistoryRef.current
    if (container) container.scrollTop = container.scrollHeight
  }, [transcriptHistory.length])

  useEffect(() => () => {
    recordingRef.current = false
    workletNodeRef.current?.disconnect()
    sourceNodeRef.current?.disconnect()
    silentGainRef.current?.disconnect()
    void audioContextRef.current?.close()
    socketRef.current?.close()
    streamRef.current?.getTracks().forEach((track) => track.stop())
    pollTimers.current.forEach((timer) => window.clearTimeout(timer))
  }, [])

  useEffect(() => {
    if (!meeting) {
      setDocs([])
      return
    }
    let disposed = false
    void getMeetingDocuments(meeting.meeting_id)
      .then(({ items: persisted }) => {
        if (disposed) return
        setDocs(persisted.map((document) => ({
          id: document.document_id,
          name: document.file_name,
          size: '已保存',
          detail: document.error || (document.status === 'completed' ? '已建立索引' : document.status),
          status: document.status === 'completed' ? 'ready' : document.status === 'failed' ? 'failed' : 'loading',
        })))
      })
      .catch((error) => { if (!disposed) setPageError(errorMessage(error)) })
    return () => { disposed = true }
  }, [meeting?.meeting_id])

  const upsertSegment = (segment: TranscriptSegment) => {
    setItems((current) => {
      const nextItem = {
        id: segment.segment_id,
        speaker: segment.speaker_name,
        initials: segment.speaker_name.slice(0, 1),
        color: 'blue',
        timestamp: timestamp(segment.start_ms),
        content: segment.text,
      } as const
      const index = current.findIndex((item) => item.id === segment.segment_id)
      if (index < 0) return [...current, nextItem]
      return current.map((item, itemIndex) => itemIndex === index ? { ...item, ...nextItem } : item)
    })
    setLastSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
  }

  const stopPcmCapture = async () => {
    const context = audioContextRef.current
    workletNodeRef.current?.port.close()
    workletNodeRef.current?.disconnect()
    sourceNodeRef.current?.disconnect()
    silentGainRef.current?.disconnect()
    streamRef.current?.getTracks().forEach((track) => track.stop())
    workletNodeRef.current = null
    sourceNodeRef.current = null
    silentGainRef.current = null
    streamRef.current = null
    audioContextRef.current = null
    if (context && context.state !== 'closed') await context.close()
  }

  const startPcmCapture = async (socket: WebSocket) => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      video: false,
    })
    const context = new AudioContext({ sampleRate: asrSampleRate })
    streamRef.current = stream
    audioContextRef.current = context
    await context.audioWorklet.addModule('/pcm-capture-worklet.js')
    const source = context.createMediaStreamSource(stream)
    sourceNodeRef.current = source
    const worklet = new AudioWorkletNode(context, 'pcm-capture-processor', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      processorOptions: { frameSize: Math.round(context.sampleRate / 10) },
    })
    workletNodeRef.current = worklet
    const silentGain = context.createGain()
    silentGainRef.current = silentGain
    silentGain.gain.value = 0
    worklet.port.onmessage = (event: MessageEvent<Float32Array>) => {
      if (!recordingRef.current || socket.readyState !== WebSocket.OPEN) return
      socket.send(encodePcm16(event.data, context.sampleRate))
    }
    source.connect(worklet)
    worklet.connect(silentGain)
    silentGain.connect(context.destination)
    await context.resume()
  }

  const waitForSocketReady = (socket: WebSocket) => new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => cleanup(new Error('流式ASR初始化超时')), 120_000)
    function cleanup(error?: Error) {
      window.clearTimeout(timeout)
      socket.removeEventListener('message', onMessage)
      error ? reject(error) : resolve()
    }
    function onMessage(event: MessageEvent) {
      try {
        const payload = JSON.parse(String(event.data)) as { type?: string; message?: string }
        if (payload.type === 'ready') cleanup()
        if (payload.type === 'error') cleanup(new Error(payload.message || '流式ASR初始化失败'))
      } catch { /* 常驻监听器会忽略无法识别的消息 */ }
    }
    socket.addEventListener('message', onMessage)
  })

  const finishStreamingSocket = (socket: WebSocket) => new Promise<void>((resolve, reject) => {
    if (socket.readyState !== WebSocket.OPEN) return resolve()
    const timeout = window.setTimeout(() => cleanup(new Error('等待流式ASR结束超时')), 60_000)
    function cleanup(error?: Error) {
      window.clearTimeout(timeout)
      socket.removeEventListener('message', onMessage)
      error ? reject(error) : resolve()
    }
    function onMessage(event: MessageEvent) {
      try {
        const payload = JSON.parse(String(event.data)) as { type?: string; message?: string }
        if (payload.type === 'finished') cleanup()
        if (payload.type === 'error') cleanup(new Error(payload.message || '流式ASR结束失败'))
      } catch { /* 常驻监听器会忽略无法识别的消息 */ }
    }
    socket.addEventListener('message', onMessage)
    socket.send(JSON.stringify({ type: 'finish' }))
  })

  const beginRecording = async () => {
    if (!meeting || starting || recordingRef.current) return
    if (!navigator.mediaDevices?.getUserMedia || typeof AudioContext === 'undefined' || typeof AudioWorkletNode === 'undefined') {
      setPageError('当前浏览器不支持AudioWorklet流式录音，请使用最新版Chrome或Edge。')
      return
    }
    setStarting(true)
    setPageError('')
    try {
      let socket = socketRef.current
      if (!sessionIdRef.current || !socket || socket.readyState !== WebSocket.OPEN) {
        const session = await startMeetingRecording(meeting.meeting_id, 'audio/pcm;rate=16000')
        sessionIdRef.current = session.session_id
        socket = createRecordingSocket(session.websocket_url)
        socketRef.current = socket
        const activeSocket: WebSocket = socket
        const readyPromise = waitForSocketReady(activeSocket)
        activeSocket.addEventListener('message', (event) => {
          try {
            const payload = JSON.parse(String(event.data)) as { type?: string; message?: string } & TranscriptSegment
            if (payload.type === 'partial' || payload.type === 'final') {
              upsertSegment(payload)
              setLiveSegmentId(payload.type === 'partial' ? payload.segment_id : null)
            }
            if (payload.type === 'error') setPageError(payload.message || '实时转写失败')
          } catch { /* 忽略不可识别的服务端事件 */ }
        })
        activeSocket.addEventListener('close', () => { if (recordingRef.current) setPageError('实时转写连接已关闭。') })
        const openPromise = new Promise<void>((resolve, reject) => {
          const timer = window.setTimeout(() => reject(new Error('实时转写连接超时')), 8_000)
          activeSocket.addEventListener('open', () => { window.clearTimeout(timer); resolve() }, { once: true })
          activeSocket.addEventListener('error', () => { window.clearTimeout(timer); reject(new Error('实时转写连接失败')) }, { once: true })
        })
        await Promise.all([openPromise, readyPromise])
        const existing = await getRecordingSegments(session.session_id)
        existing.forEach(upsertSegment)
      }
      setRecordingState(true)
      await startPcmCapture(socket)
    } catch (error) {
      setRecordingState(false)
      setPageError(errorMessage(error))
      await stopPcmCapture()
    } finally {
      setStarting(false)
    }
  }

  const pauseRecording = async () => {
    setRecordingState(false)
    await stopPcmCapture()
  }

  const updateDocument = (id: string, patch: Partial<MeetingDocument>) => {
    setDocs((current) => current.map((doc) => doc.id === id ? { ...doc, ...patch } : doc))
  }

  const pollImportTask = async (documentId: string, taskId: string) => {
    try {
      const task = await getImportTaskStatus(taskId)
      const latestStage = task.running_list[0] || task.done_list.at(-1) || '等待处理'
      if (task.status === 'completed') return updateDocument(documentId, { detail: '已建立索引', status: 'ready' })
      if (task.status === 'failed') return updateDocument(documentId, { detail: '处理失败', status: 'failed' })
      updateDocument(documentId, { detail: latestStage, status: 'loading' })
      const timer = window.setTimeout(() => {
        pollTimers.current.delete(timer)
        void pollImportTask(documentId, taskId)
      }, 1200)
      pollTimers.current.add(timer)
    } catch (error) {
      updateDocument(documentId, { detail: errorMessage(error), status: 'failed' })
    }
  }

  const uploadFiles = async (selectedFiles: File[]) => {
    if (!meeting) return setPageError('请先选择会议。')
    const accepted = selectedFiles.filter((file) => supportedExtensions.has(file.name.split('.').pop()?.toLowerCase() || '') && file.size <= maxFileSize)
    if (!accepted.length) return setPageError('仅支持 PDF、PPTX、DOCX、Markdown，单个文件最大 100 MB。')
    setPageError(accepted.length === selectedFiles.length ? '' : '已跳过格式不支持或超过 100 MB 的文件。')
    const pending = accepted.map((file, index): MeetingDocument => ({ id: `upload-${Date.now()}-${index}`, name: file.name, size: fileSizeLabel(file.size), detail: '正在上传', status: 'loading' }))
    setDocs((current) => [...current, ...pending])
    try {
      const result = await uploadDocuments(accepted, meeting.meeting_id)
      pending.forEach((doc, index) => {
        const taskId = result.task_ids[index]
        if (!taskId) return updateDocument(doc.id, { detail: '后端未返回任务 ID', status: 'failed' })
        updateDocument(doc.id, { taskId, detail: '等待处理' })
        void pollImportTask(doc.id, taskId)
      })
    } catch (error) {
      const message = errorMessage(error)
      pending.forEach((doc) => updateDocument(doc.id, { detail: message, status: 'failed' }))
      setPageError(message)
    }
  }

  const endMeeting = async () => {
    const sessionId = sessionIdRef.current
    if (!meeting) return setPageError('请先选择会议。')
    setProcessing(true)
    setStep(1)
    setCompletionHasTranscript(null)
    setPageError('')
    try {
      if (!sessionId) {
        await updateMeetingStatus(meeting.meeting_id, 'completed')
        setCompletionHasTranscript(false)
      } else {
        await pauseRecording()
        const socket = socketRef.current
        if (socket?.readyState === WebSocket.OPEN) await finishStreamingSocket(socket)
        setStep(2)
        const result = await endMeetingRecording(sessionId)
        socketRef.current?.close()
        socketRef.current = null
        setCompletionHasTranscript(result.transcript_generated)
        if (result.transcript_generated) {
          setStep(3)
          while (true) {
            const task = await getRecordingImportStatus(sessionId)
            if (task.status === 'completed') break
            if (task.status === 'failed') throw new Error('Markdown 已生成，但 Import Graph 导入失败。')
            await new Promise((resolve) => window.setTimeout(resolve, 1200))
          }
        }
      }
      setStep(4)
      streamRef.current?.getTracks().forEach((track) => track.stop())
      streamRef.current = null
    } catch (error) {
      setPageError(errorMessage(error))
      setProcessing(false)
      setShowEnd(false)
    }
  }

  const time = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
  const expectsTranscript = completionHasTranscript ?? items.length > 0
  const processLabels = expectsTranscript
    ? ['结束实时 PCM 流', '生成 Markdown', 'Import Graph 入库']
    : ['检查录音状态', '结束会议', '无真实转写，跳过文档']
  const hasRecordingStarted = seconds > 0 || items.length > 0 || Boolean(sessionIdRef.current)
  const recordingStatus = starting ? '正在连接转写服务' : recording ? '正在记录' : hasRecordingStarted ? '记录已暂停' : '准备就绪'
  const scheduledLabel = meeting
    ? new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(meeting.scheduled_start))
    : '请选择一场会议'

  return <div className="live-page">
    <header className="meeting-header">
      <button className="icon-button meeting-back-button" onClick={() => onNavigate('home')} aria-label="返回首页"><ArrowLeft size={19} /></button>
      <div className="meeting-header__title">
        <span className={`meeting-kicker ${recording ? 'meeting-kicker--live' : ''}`}><i />{recording ? '会议进行中' : hasRecordingStarted ? '会议已暂停' : '会议工作台'}</span>
        <h1>{meeting?.title || '会议工作台'}</h1>
        <div className="meeting-header__meta"><span><CalendarDays size={13} />{scheduledLabel}</span>{meeting?.location && <span><MapPin size={13} />{meeting.location}</span>}</div>
      </div>
      <div className="meeting-header__people"><div className="mini-avatars mini-avatars--header"><i>我</i></div><div><strong>{meeting?.participant_count || 1}</strong><span>位参会者</span></div></div>
      <button className="end-button" onClick={() => setShowEnd(true)}><CircleStop size={16} />结束会议</button>
    </header>

    <div className="meeting-layout">
      <section className="transcript-panel">
        <div className="transcript-toolbar">
          <div><span className="section-index">01</span><div><h2>实时纪要</h2><p>专注当前发言，已确认内容会自动沉淀</p></div></div>
          <span className={`asr-state ${recording ? 'asr-state--live' : ''}`}><Radio size={13} />{recording ? '正在识别' : '等待开始'}</span>
        </div>
        {pageError && <p className="upload-error" role="alert">{pageError}</p>}
        <section className={`live-caption-stage ${liveTranscript ? 'live-caption-stage--active' : ''} ${visibleLiveCaption?.truncated ? 'live-caption-stage--truncated' : ''}`} aria-live="polite">
          <div className="live-caption-stage__meta"><span><i />{liveTranscript ? '正在转写' : recording ? '聆听中' : '当前发言'}</span>{liveTranscript && <time className="mono">{liveTranscript.timestamp}</time>}</div>
          {liveTranscript && visibleLiveCaption ? <><p>{visibleLiveCaption.text}<i className="typing-caret" /></p>{visibleLiveCaption.truncated && <span className="live-caption-stage__limit">较早内容已收起，完整转写已保留</span>}</> : <div className="live-caption-stage__placeholder"><span className="caption-mic"><Mic2 size={20} /></span><div><strong>{recording ? '正在聆听' : '从一次清晰的记录开始'}</strong><span>{recording ? '开始发言后，最新一句会显示在这里' : '点击下方“开始记录”，实时纪要会自动出现'}</span></div></div>}
        </section>
        <div className="transcript-history-wrap">
          <div className="transcript-history__title"><span>已确认内容</span><small>{transcriptHistory.length ? `${transcriptHistory.length} 条记录` : '尚无内容'}</small></div>
          <div className="transcript-history" ref={transcriptHistoryRef}>
            {transcriptHistory.map((item) => <article className="transcript-history__item" key={item.id}><time className="mono">{item.timestamp}</time><p>{item.content}</p></article>)}
            {!items.length && !liveTranscript && <div className="transcript-empty"><span>记录会按时间顺序出现在这里</span><i /></div>}
          </div>
        </div>
        <div className="recording-console">
          <div className="recording-state"><span className={recording ? 'record-pulse' : 'record-paused'} /><div><strong>{recordingStatus}</strong><span>{recording ? '实时语音正在安全传输' : hasRecordingStarted ? '随时可以继续本次记录' : '麦克风将在开始后启用'}</span></div></div>
          <div className="waveform" aria-label="音频录制状态">{waveform.map((height, index) => <i key={index} className={!recording ? 'paused' : ''} style={{ height: `${height}px`, animationDelay: `${(index % 10) * 60}ms` }} />)}</div>
          <div className="recording-time"><span className="mono">{time}</span><small>{lastSavedAt ? `已保存 ${lastSavedAt}` : '有效记录时长'}</small></div>
          <button className={`record-toggle ${recording ? 'record-toggle--active' : ''}`} disabled={starting || !meeting} onClick={() => recording ? void pauseRecording() : void beginRecording()} aria-label={recording ? '暂停记录' : '开始记录'}>{recording ? <Pause size={17} fill="currentColor" /> : <Mic2 size={17} />}<span>{starting ? '连接中' : recording ? '暂停记录' : hasRecordingStarted ? '继续记录' : '开始记录'}</span></button>
        </div>
      </section>

      <aside className="meeting-side-panel">
        <section className="meeting-overview">
          <div className="meeting-overview__heading"><span>本场会议</span><i className={recording ? 'is-live' : ''}>{recording ? 'LIVE' : 'READY'}</i></div>
          <div className="meeting-overview__person"><span>{user.display_name.slice(0, 1)}</span><div><strong>{user.display_name}</strong><small>会议主持人</small></div><em>在线</em></div>
          <div className="meeting-overview__stats"><div><strong className="mono">{time}</strong><span>记录时长</span></div><div><strong>{items.length}</strong><span>转写条目</span></div><div><strong>{docs.length}</strong><span>会议资料</span></div></div>
        </section>
        <div className="meeting-tabs"><div><span className="section-index">02</span><div><strong>会议资料</strong><small>供会后检索与问答使用</small></div></div><button onClick={() => fileInput.current?.click()}><Plus size={15} />添加</button></div>
        <div className="upload-zone" onClick={() => fileInput.current?.click()} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') fileInput.current?.click() }} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); void uploadFiles(Array.from(event.dataTransfer.files)) }} role="button" tabIndex={0}>
          <input ref={fileInput} type="file" hidden multiple onChange={(event) => { const files = Array.from(event.target.files || []); event.target.value = ''; if (files.length) void uploadFiles(files) }} accept=".pdf,.docx,.pptx,.md" />
          <span><UploadCloud size={19} /></span><div><strong>上传会议资料</strong><p>拖拽到这里，或点击选择文件</p></div>
        </div>
        <div className="document-list">{docs.map((doc, index) => <article className="document-item" key={doc.id}><div className={`file-icon ${index % 2 ? 'file-icon--blue' : 'file-icon--red'}`}><FileText size={18} /></div><div><strong>{doc.name}</strong><span>{doc.size} · {doc.detail}</span></div>{doc.status === 'loading' ? <span className="mini-loader" /> : doc.status === 'failed' ? <span className="document-error" title={doc.detail}><X size={15} /></span> : <button className="icon-button" aria-label={`更多：${doc.name}`}><MoreHorizontal size={17} /></button>}</article>)}</div>
        {!docs.length && <div className="document-empty"><FileText size={21} /><strong>还没有会议资料</strong><span>上传议程或背景文档，会议结束后即可直接提问。</span></div>}
        <footer className="side-panel-note"><Check size={13} /><span>资料将自动建立索引</span></footer>
      </aside>
    </div>

    {showEnd && <div className="modal-layer"><button className="modal-backdrop" onClick={() => !processing && setShowEnd(false)} aria-label="关闭" /><section className="end-modal">{!processing ? <><button className="icon-button modal-close" onClick={() => setShowEnd(false)}><X size={19} /></button><span className="modal-hero-icon"><CircleStop size={25} /></span><h2>结束本次会议？</h2><p>{items.length ? '系统会停止麦克风，生成最终转写 Markdown，并自动调用 Import Graph 入库。' : '系统会结束会议；若没有真实 ASR 转写，将跳过转录 Markdown 与 Import Graph。'}</p><div className="end-summary"><span><Clock3 size={17} />有效录音<strong>{time}</strong></span><span><File size={17} />会议资料<strong>{docs.length} 份</strong></span><span><Users size={17} />参会成员<strong>{meeting?.participant_count || 1} 人</strong></span></div><div className="modal-actions"><button onClick={() => setShowEnd(false)}>继续会议</button><button className="primary-button" onClick={() => void endMeeting()}>{items.length ? '结束并导入' : '结束会议'}</button></div></> : <><span className={`processing-orb ${step === 4 ? 'processing-orb--done' : ''}`}>{step === 4 ? <Check size={28} /> : <Sparkles size={25} />}</span><h2>{step === 4 ? (completionHasTranscript ? '会议与转写已完成' : '会议已结束') : '正在结束会议'}</h2><p>{step === 4 ? (completionHasTranscript ? 'Markdown 与向量索引均已完成。' : '未检测到真实转写，已跳过 Markdown 生成与导入。') : '正在停止录音并保存会议状态，请保持页面打开。'}</p><div className="process-steps">{processLabels.map((label, index) => <div key={label} className={step > index + 1 ? 'done' : step === index + 1 ? 'active' : ''}><span>{step > index + 1 ? <Check size={13} /> : index + 1}</span><strong>{label}</strong>{step === index + 1 && step < 4 && <i />}</div>)}</div>{step === 4 && <button className="primary-button modal-finish" onClick={() => onNavigate('meetings')}>查看会议记录</button>}</>}</section></div>}
  </div>
}

import { useEffect, useState } from 'react'
import { AppShell } from './components/AppShell'
import { CreateMeetingDialog } from './components/CreateMeetingDialog'
import { HomePage } from './pages/HomePage'
import { LiveMeetingPage } from './pages/LiveMeetingPage'
import { MeetingsPage } from './pages/MeetingsPage'
import { RagPage } from './pages/RagPage'
import { getLocalProfile, setActiveMeeting, updateMeetingStatus } from './api/backend'
import type { LocalUser, Meeting, View } from './types'

const validViews: View[] = ['home', 'live', 'meetings', 'rag']

export function parseViewHash(hash: string): View {
  const normalized = hash.replace(/^#\/?/, '').split('?')[0].toLowerCase() as View
  return validViews.includes(normalized) ? normalized : 'home'
}

function readViewFromHash(): View {
  return parseViewHash(window.location.hash)
}

export default function App() {
  const [user, setUser] = useState<LocalUser>({ display_name: '老己' })
  const [active, setActive] = useState<View>(readViewFromHash)
  const [activeMeeting, setActiveMeetingState] = useState<Meeting | null>(null)
  const [showCreateMeeting, setShowCreateMeeting] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    const onHashChange = () => setActive(readViewFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  useEffect(() => {
    let disposed = false
    void getLocalProfile()
      .then((profile) => { if (!disposed && profile.display_name.trim()) setUser(profile) })
      .catch(() => { /* 保留前端默认称呼“老己” */ })
    return () => { disposed = true }
  }, [])

  const navigate = (view: View) => {
    if (view === 'live' && !activeMeeting) {
      setShowCreateMeeting(true)
      return
    }
    setActive(view)
    if (window.location.hash !== `#/${view}`) window.location.hash = `/${view}`
    document.documentElement.scrollTop = 0
    document.body.scrollTop = 0
  }

  const enterMeeting = async (meeting: Meeting) => {
    let selected = meeting
    if (meeting.status === 'scheduled') {
      try {
        selected = await updateMeetingStatus(meeting.meeting_id, 'in_progress')
      } catch {
        selected = meeting
      }
    }
    setActiveMeeting(selected)
    setActiveMeetingState(selected)
    setShowCreateMeeting(false)
    setActive('live')
    window.location.hash = '/live'
  }

  return (
    <>
      <AppShell
        active={active}
        onNavigate={navigate}
        sidebarOpen={sidebarOpen}
        setSidebarOpen={setSidebarOpen}
        onCreateMeeting={() => setShowCreateMeeting(true)}
        user={user}
      >
        {active === 'home' && <HomePage user={user} onNavigate={navigate} onCreateMeeting={() => setShowCreateMeeting(true)} onEnterMeeting={enterMeeting} />}
        {active === 'live' && <LiveMeetingPage onNavigate={navigate} meeting={activeMeeting} user={user} />}
        {active === 'meetings' && <MeetingsPage onNavigate={navigate} onCreateMeeting={() => setShowCreateMeeting(true)} onEnterMeeting={enterMeeting} user={user} />}
        {active === 'rag' && <RagPage />}
      </AppShell>
      {showCreateMeeting && <CreateMeetingDialog onClose={() => setShowCreateMeeting(false)} onCreated={enterMeeting} />}
    </>
  )
}

import type { ReactNode } from 'react'
import {
  Bell, Bot, ChevronDown, CircleHelp, Home, Menu, MessageSquareText,
  Plus, Search, Settings, Sparkles, Video, X,
} from 'lucide-react'
import { Brand } from './Brand'
import type { LocalUser, View } from '../types'

const navItems: Array<{ id: View; label: string; icon: typeof Home }> = [
  { id: 'home', label: '首页', icon: Home },
  { id: 'live', label: '会议工作台', icon: Video },
  { id: 'meetings', label: '会议记录', icon: MessageSquareText },
  { id: 'rag', label: '会议问答', icon: Bot },
]

interface AppShellProps {
  active: View
  onNavigate: (view: View) => void
  onCreateMeeting: () => void
  user: LocalUser
  children: ReactNode
  sidebarOpen: boolean
  setSidebarOpen: (value: boolean) => void
}

export function AppShell({ active, onNavigate, onCreateMeeting, user, children, sidebarOpen, setSidebarOpen }: AppShellProps) {
  const navigate = (view: View) => {
    onNavigate(view)
    setSidebarOpen(false)
  }

  return (
    <div className="app-shell">
      {sidebarOpen && <button className="mobile-backdrop" aria-label="关闭菜单" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? 'sidebar--open' : ''}`}>
        <div className="sidebar__top">
          <Brand />
          <button className="icon-button sidebar__close" onClick={() => setSidebarOpen(false)} aria-label="关闭菜单"><X size={19} /></button>
        </div>

        <button className="new-meeting-button" onClick={onCreateMeeting}>
          <Plus size={18} />
          <span>发起新会议</span>
        </button>

        <nav className="sidebar__nav" aria-label="主导航">
          <p className="nav-label">工作空间</p>
          {navItems.map(({ id, label, icon: Icon }) => (
            <button key={id} className={`nav-item ${active === id ? 'nav-item--active' : ''}`} onClick={() => navigate(id)}>
              <Icon size={19} strokeWidth={active === id ? 2.2 : 1.8} />
              <span>{label}</span>
              {id === 'live' && <span className="nav-live-dot" />}
            </button>
          ))}
        </nav>

        <div className="sidebar__insight">
          <span className="sidebar__insight-icon"><Sparkles size={16} /></span>
          <div>
            <strong>本周洞察已生成</strong>
            <p>查看 5 场会议的共同议题</p>
          </div>
        </div>

        <div className="sidebar__bottom">
          <button className="nav-item"><Settings size={18} /><span>设置</span></button>
          <button className="nav-item"><CircleHelp size={18} /><span>帮助与反馈</span></button>
          <div className="profile-menu">
            <div className="avatar avatar--dark">{user.display_name.slice(0, 1)}</div>
            <div className="profile-menu__text"><strong>{user.display_name}</strong><span>本地会议空间</span></div>
          </div>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <button className="icon-button topbar__menu" onClick={() => setSidebarOpen(true)} aria-label="打开菜单"><Menu size={20} /></button>
          <div className="global-search">
            <Search size={17} />
            <input aria-label="全局搜索" placeholder="搜索会议、发言或文档" />
            <kbd>⌘ K</kbd>
          </div>
          <div className="topbar__actions">
            <button className="icon-button notification-button" aria-label="通知"><Bell size={19} /><span /></button>
            <button className="workspace-switcher"><span className="avatar avatar--small">{user.display_name.slice(0, 1)}</span><span>本地会议空间</span><ChevronDown size={15} /></button>
          </div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  )
}

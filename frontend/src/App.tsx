import React from 'react'
import { Routes, Route, Link, useLocation } from 'react-router-dom'
import RunList from './pages/RunList'
import RunDetail from './pages/RunDetail'
import Dashboard from './pages/Dashboard'
import ToolsPanel from './pages/ToolsPanel'
import GuardrailPanel from './pages/GuardrailPanel'
import RunAnalysis from './pages/RunAnalysis'
import OpsDashboard from './pages/OpsDashboard'
import OpsRunDetail from './pages/OpsRunDetail'
import OpsSystem from './pages/OpsSystem'
import OpsChatView from './pages/OpsChatView'
import { colors } from './api/analysis-styles'

const styles: Record<string, React.CSSProperties> = {
  header: {
    background: '#1a1a2e',
    color: '#eee',
    padding: '12px 24px',
    display: 'flex',
    alignItems: 'center',
    gap: 16,
  },
  container: {
    maxWidth: 1200,
    margin: '0 auto',
    padding: 24,
  },
}

function NavLink({ to, label }: { to: string; label: string }) {
  const location = useLocation()
  const isActive = location.pathname === to || location.pathname.startsWith(to + '/')
  return (
    <Link
      to={to}
      style={{
        color: isActive ? colors.primary : '#bbb',
        textDecoration: 'none',
        fontSize: 13,
        fontWeight: 500,
        padding: '4px 10px',
        borderRadius: 4,
        background: isActive ? 'rgba(62,207,142,0.15)' : 'transparent',
        transition: 'all 0.15s',
      }}
    >
      {label}
    </Link>
  )
}

export default function App() {
  return (
    <div>
      <header style={styles.header}>
        <Link to="/analysis" style={{ color: '#eee', textDecoration: 'none', fontSize: 20, fontWeight: 'bold' }}>
          Harness
        </Link>
        <span style={{ fontSize: 12, opacity: 0.6 }}>Agent-First Task Execution Engine</span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <NavLink to="/analysis" label="Dashboard" />
          <NavLink to="/analysis/tools" label="Tools" />
          <NavLink to="/analysis/guardrails" label="Guardrails" />
          <NavLink to="/" label="Runs" />
          <span style={{ color: '#444', fontSize: 13 }}>|</span>
          <NavLink to="/ops" label="Ops" />
          <NavLink to="/ops/system" label="System" />
        </div>
      </header>
      <div style={styles.container}>
        <Routes>
          <Route path="/" element={<RunList />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/analysis" element={<Dashboard />} />
          <Route path="/analysis/tools" element={<ToolsPanel />} />
          <Route path="/analysis/guardrails" element={<GuardrailPanel />} />
          <Route path="/analysis/runs/:runId" element={<RunAnalysis />} />
          <Route path="/ops" element={<OpsDashboard />} />
          <Route path="/ops/chat" element={<OpsChatView />} />
          <Route path="/ops/runs/:runId" element={<OpsRunDetail />} />
          <Route path="/ops/system" element={<OpsSystem />} />
        </Routes>
      </div>
    </div>
  )
}

import React from 'react'
import { Routes, Route, Link } from 'react-router-dom'
import RunList from './pages/RunList'
import RunDetail from './pages/RunDetail'

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

export default function App() {
  return (
    <div>
      <header style={styles.header}>
        <Link to="/" style={{ color: '#eee', textDecoration: 'none', fontSize: 20, fontWeight: 'bold' }}>
          Harness
        </Link>
        <span style={{ fontSize: 12, opacity: 0.6 }}>Agent-First Task Execution Engine</span>
      </header>
      <div style={styles.container}>
        <Routes>
          <Route path="/" element={<RunList />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
        </Routes>
      </div>
    </div>
  )
}

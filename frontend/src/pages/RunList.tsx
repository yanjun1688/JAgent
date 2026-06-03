import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listRuns, createRun, RunSummary } from '../api/client'

const STATUS_COLORS: Record<string, string> = {
  running: '#4fc3f7',
  paused: '#ffb74d',
  completed: '#66bb6a',
  failed: '#ef5350',
}

export default function RunList() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [intent, setIntent] = useState('')
  const [loading, setLoading] = useState(true)

  async function load() {
    setLoading(true)
    try {
      const data = await listRuns()
      setRuns(data.runs)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  async function handleCreate() {
    if (!intent.trim()) return
    await createRun(intent.trim())
    setIntent('')
    await load()
  }

  function formatTime(ts: number): string {
    return new Date(ts * 1000).toLocaleString()
  }

  return (
    <div>
      <h1>Runs</h1>

      <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
        <input
          value={intent}
          onChange={(e) => setIntent(e.target.value)}
          placeholder="Task intent (e.g. search the web for...)"
          style={{
            flex: 1,
            padding: '8px 12px',
            border: '1px solid #ccc',
            borderRadius: 4,
            fontSize: 14,
          }}
          onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
        />
        <button
          onClick={handleCreate}
          style={{
            padding: '8px 20px',
            background: '#1a73e8',
            color: '#fff',
            border: 'none',
            borderRadius: 4,
            cursor: 'pointer',
          }}
        >
          New Run
        </button>
      </div>

      {loading ? (
        <p>Loading...</p>
      ) : runs.length === 0 ? (
        <p style={{ color: '#888' }}>No runs yet. Create one above.</p>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ textAlign: 'left', borderBottom: '2px solid #eee' }}>
              <th style={{ padding: 8 }}>Run ID</th>
              <th style={{ padding: 8 }}>Intent</th>
              <th style={{ padding: 8 }}>Status</th>
              <th style={{ padding: 8 }}>Events</th>
              <th style={{ padding: 8 }}>Created</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id} style={{ borderBottom: '1px solid #eee' }}>
                <td style={{ padding: 8 }}>
                  <Link to={`/runs/${run.run_id}`} style={{ color: '#1a73e8' }}>
                    {run.run_id}
                  </Link>
                </td>
                <td style={{ padding: 8, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {run.intent || '(no intent)'}
                </td>
                <td style={{ padding: 8 }}>
                  <span
                    style={{
                      display: 'inline-block',
                      padding: '2px 8px',
                      borderRadius: 12,
                      fontSize: 12,
                      fontWeight: 'bold',
                      color: '#fff',
                      background: STATUS_COLORS[run.status ?? ''] || '#999',
                    }}
                  >
                    {run.status}
                  </span>
                </td>
                <td style={{ padding: 8 }}>{run.event_count}</td>
                <td style={{ padding: 8 }}>{formatTime(run.created_at ?? 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

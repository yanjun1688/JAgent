import React, { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { querySystem } from "../api/ops-client"
import type { SystemData } from "../api/ops-client"
import {
  card,
  sectionTitle,
  colors,
  fmt,
  table,
  th,
  td,
  badge,
} from "../api/analysis-styles"

export default function OpsSystem() {
  const [data, setData] = useState<SystemData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    querySystem()
      .then((res) => {
        setData(res.data)
        setError(null)
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <p style={{ color: colors.textSecondary, fontSize: 13 }}>Loading system config...</p>
  }

  if (error) {
    return <p style={{ color: colors.red }}>{error}</p>
  }

  if (!data) {
    return <p>No system data available</p>
  }

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <Link to="/ops" style={{ color: colors.blue, textDecoration: "none", fontSize: 14 }}>
          ← Ops Dashboard
        </Link>
      </div>

      <h1 style={{ margin: "0 0 20px", fontSize: 20, fontWeight: 700 }}>System Configuration</h1>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 24 }}>
        <div style={{ ...card, flex: "1 1 400px", minWidth: 320 }}>
          <div style={sectionTitle}>LLM Client</div>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Property</th>
                <th style={th}>Value</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td style={{ ...td, fontWeight: 600 }}>Type</td>
                <td style={td}>{data.llm_client.type}</td>
              </tr>
              <tr>
                <td style={{ ...td, fontWeight: 600 }}>Model</td>
                <td style={td}>{data.llm_client.model}</td>
              </tr>
              <tr>
                <td style={{ ...td, fontWeight: 600 }}>Base URL</td>
                <td style={{ ...td, fontFamily: "monospace", fontSize: 12 }}>{data.llm_client.base_url}</td>
              </tr>
              <tr>
                <td style={{ ...td, fontWeight: 600 }}>Total Calls</td>
                <td style={td}>{fmt(data.llm_client.total_calls)}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div style={{ ...card, flex: "1 1 400px", minWidth: 320 }}>
          <div style={sectionTitle}>Tool Registry</div>
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Property</th>
                <th style={th}>Value</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td style={{ ...td, fontWeight: 600 }}>Registered Tools</td>
                <td style={td}>{fmt(data.tool_registry.tool_count)}</td>
              </tr>
              <tr>
                <td style={{ ...td, fontWeight: 600 }}>Tool Defs Count</td>
                <td style={td}>{fmt(data.tool_defs_count)}</td>
              </tr>
            </tbody>
          </table>
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6, color: colors.textSecondary }}>Tool Names</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {data.tool_registry.tool_names.length === 0 ? (
                <span style={{ color: colors.textSecondary, fontSize: 13 }}>None</span>
              ) : (
                data.tool_registry.tool_names.map((name) => (
                  <span key={name} style={badge(colors.blue, "#fff")}>
                    {name}
                  </span>
                ))
              )}
            </div>
          </div>
        </div>

        <div style={{ ...card, flex: "1 1 400px", minWidth: 320 }}>
          <div style={sectionTitle}>Scheduler Default Config</div>
          {Object.keys(data.scheduler_config).length === 0 ? (
            <p style={{ color: colors.textSecondary, fontSize: 13 }}>No config available</p>
          ) : (
            <table style={table}>
              <thead>
                <tr>
                  <th style={th}>Key</th>
                  <th style={th}>Value</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.scheduler_config).map(([key, val]) => (
                  <tr key={key}>
                    <td style={{ ...td, fontWeight: 600 }}>{key}</td>
                    <td style={{ ...td, fontFamily: "monospace", fontSize: 12 }}>
                      {typeof val === "object" ? JSON.stringify(val) : String(val)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}

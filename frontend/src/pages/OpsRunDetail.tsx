import { useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'

export default function OpsRunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()

  useEffect(() => {
    if (runId) {
      navigate(`/ops/chat?runId=${encodeURIComponent(runId)}`, { replace: true })
    } else {
      navigate('/ops/chat', { replace: true })
    }
  }, [runId, navigate])

  return null
}

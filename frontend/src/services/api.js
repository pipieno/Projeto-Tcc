const BASE = import.meta.env.VITE_API_URL ?? ''

export async function sendMessage(sessionId, message) {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function exportSession(sessionId) {
  const res = await fetch(`${BASE}/export/${sessionId}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

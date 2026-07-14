const BASE = import.meta.env.VITE_API_URL ?? ''

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, options)

  if (!res.ok) {
    let message = `HTTP ${res.status}`

    try {
      const body = await res.json()
      message = body.detail ?? body.message ?? message
    } catch {
      // A resposta de erro pode não ter corpo JSON.
    }

    throw new ApiError(message, res.status)
  }

  return res.json()
}

export function isInvalidSessionError(error) {
  return error instanceof ApiError && (error.status === 401 || error.status === 404)
}

export function createSession() {
  return request('/sessions', { method: 'POST' })
}

export function sendMessage(sessionId, sessionToken, message) {
  return request('/chat', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${sessionToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ session_id: sessionId, message }),
  })
}

export function exportSession(sessionId, sessionToken) {
  return request(`/export/${encodeURIComponent(sessionId)}`, {
    headers: { Authorization: `Bearer ${sessionToken}` },
  })
}

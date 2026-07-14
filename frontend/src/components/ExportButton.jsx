import { exportSession, isInvalidSessionError } from '../services/api'

function isSessionValid(sessionId, sessionToken, sessionExpiresAt) {
  const expiresAt = Date.parse(sessionExpiresAt)
  return Boolean(
    sessionId
    && sessionToken
    && !Number.isNaN(expiresAt)
    && expiresAt > Date.now()
  )
}

export default function ExportButton({
  sessionId,
  sessionToken,
  sessionExpiresAt,
  messages,
  onSessionInvalid,
}) {
  const canExport = isSessionValid(sessionId, sessionToken, sessionExpiresAt)

  async function handleExport(format) {
    if (!isSessionValid(sessionId, sessionToken, sessionExpiresAt)) {
      onSessionInvalid?.(sessionToken)
      return
    }

    try {
      const data = await exportSession(sessionId, sessionToken)

      if (format === 'json') {
        download(JSON.stringify(data, null, 2), `chat-${sessionId}.json`, 'application/json')
        return
      }

      const header = 'role,timestamp,content\n'
      const rows = data.messages
        .map(m => `${m.role},${m.timestamp},"${m.content.replace(/"/g, '""')}"`)
        .join('\n')
      download(header + rows, `chat-${sessionId}.csv`, 'text/csv')
    } catch (error) {
      if (isInvalidSessionError(error)) {
        onSessionInvalid?.(sessionToken)
        alert('A sessão expirou. Envie uma nova mensagem para iniciar outra conversa.')
        return
      }

      alert('Erro ao exportar: ' + error.message)
    }
  }

  function download(content, filename, type) {
    const blob = new Blob([content], { type })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href     = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }

  if (!messages.length) return null

  return (
    <div className="export-wrap">
      <button className="export-btn" onClick={() => handleExport('json')} title="Exportar conversa em JSON" disabled={!canExport}>JSON</button>
      <button className="export-btn" onClick={() => handleExport('csv')} title="Exportar conversa em CSV" disabled={!canExport}>CSV</button>
    </div>
  )
}

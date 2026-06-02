import { exportSession } from '../services/api'

const btn = {
  padding: '8px 16px',
  background: 'transparent',
  border: '1px solid #1a73e8',
  color: '#1a73e8',
  borderRadius: '6px',
  cursor: 'pointer',
  fontSize: '0.8rem',
  fontWeight: 500,
}

export default function ExportButton({ sessionId, messages }) {
  async function handleExport(format) {
    try {
      const data = await exportSession(sessionId)

      if (format === 'json') {
        download(JSON.stringify(data, null, 2), `chat-${sessionId}.json`, 'application/json')
        return
      }

      const header = 'role,timestamp,content\n'
      const rows = data.messages
        .map(m => `${m.role},${m.timestamp},"${m.content.replace(/"/g, '""')}"`)
        .join('\n')
      download(header + rows, `chat-${sessionId}.csv`, 'text/csv')
    } catch (e) {
      alert('Erro ao exportar: ' + e.message)
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
    <div style={{ display: 'flex', gap: 8 }}>
      <button style={btn} onClick={() => handleExport('json')}>⬇ JSON</button>
      <button style={btn} onClick={() => handleExport('csv')}>⬇ CSV</button>
    </div>
  )
}

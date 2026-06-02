import { useState, useRef, useEffect } from 'react'
import { sendMessage } from '../services/api'
import ExportButton from './ExportButton'

const styles = {
  card: {
    width: 540, height: '90vh', maxHeight: 680,
    background: '#fff', borderRadius: 12,
    boxShadow: '0 4px 24px #0002',
    display: 'flex', flexDirection: 'column', overflow: 'hidden',
  },
  header: {
    background: '#1a73e8', color: '#fff',
    padding: '16px 20px',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  },
  headerInfo: { display: 'flex', flexDirection: 'column', gap: 2 },
  title: { fontSize: '1rem', fontWeight: 600 },
  subtitle: { fontSize: '0.75rem', opacity: 0.85 },
  messages: {
    flex: 1, overflowY: 'auto', padding: '16px',
    display: 'flex', flexDirection: 'column', gap: 10,
  },
  bubbleWrapper: (role) => ({
    maxWidth: '78%',
    alignSelf: role === 'user' ? 'flex-end' : 'flex-start',
  }),
  bubble: (role) => ({
    background: role === 'user' ? '#1a73e8' : role === 'error' ? '#fdecea' : '#f1f3f4',
    color: role === 'user' ? '#fff' : role === 'error' ? '#c62828' : '#202124',
    padding: '10px 14px',
    borderRadius: role === 'user' ? '18px 18px 4px 18px' : '18px 18px 18px 4px',
    fontSize: '0.875rem',
    lineHeight: 1.5,
    wordBreak: 'break-word',
    whiteSpace: 'pre-wrap',
  }),
  meta: { fontSize: '0.7rem', opacity: 0.55, marginTop: 4 },
  form: {
    display: 'flex', gap: 8, padding: '12px 16px',
    borderTop: '1px solid #e8eaed',
  },
  input: {
    flex: 1, padding: '10px 16px', fontSize: '0.875rem',
    border: '1px solid #dadce0', borderRadius: 24,
    outline: 'none',
  },
  sendBtn: {
    padding: '10px 20px', background: '#1a73e8', color: '#fff',
    border: 'none', borderRadius: 24, cursor: 'pointer',
    fontSize: '0.875rem', fontWeight: 500,
  },
}

function genSessionId() {
  return 'web-' + Math.random().toString(36).slice(2, 9)
}

export default function Chat() {
  const [messages, setMessages] = useState([])
  const [input, setInput]       = useState('')
  const [loading, setLoading]   = useState(false)
  const [sessionId]             = useState(genSessionId)
  const bottomRef               = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function handleSubmit(e) {
    e.preventDefault()
    const text = input.trim()
    if (!text || loading) return

    const ts = new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
    setMessages(prev => [...prev, { role: 'user', content: text, ts }])
    setInput('')
    setLoading(true)
    setMessages(prev => [...prev, { role: 'bot', content: '...', ts: '', id: 'typing' }])

    try {
      const data = await sendMessage(sessionId, text)
      setMessages(prev =>
        prev.map(m => m.id === 'typing'
          ? { role: 'bot', content: data.response, ts: new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' }) }
          : m
        )
      )
    } catch (err) {
      setMessages(prev =>
        prev.map(m => m.id === 'typing'
          ? { role: 'error', content: 'Erro: ' + err.message, ts: '' }
          : m
        )
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={styles.card}>
      <header style={styles.header}>
        <div style={styles.headerInfo}>
          <span style={styles.title}>AMOBEM</span>
          <span style={styles.subtitle}>Assistente Virtual da Associação de Moradores</span>
        </div>
        <ExportButton sessionId={sessionId} messages={messages} />
      </header>

      <div style={styles.messages}>
        {messages.length === 0 && (
          <p style={{ color: '#9aa0a6', fontSize: '0.85rem', textAlign: 'center', marginTop: 40 }}>
            Olá! Como posso ajudar você hoje?
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} style={styles.bubbleWrapper(m.role)}>
            <div style={styles.bubble(m.role)}>{m.content}</div>
            {m.ts && <div style={{ ...styles.meta, textAlign: m.role === 'user' ? 'right' : 'left' }}>{m.ts}</div>}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <form style={styles.form} onSubmit={handleSubmit}>
        <input
          style={styles.input}
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Digite sua mensagem..."
          disabled={loading}
          autoFocus
        />
        <button style={{ ...styles.sendBtn, opacity: loading ? 0.6 : 1 }} type="submit" disabled={loading}>
          Enviar
        </button>
      </form>
    </div>
  )
}

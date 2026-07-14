import { useState, useRef, useEffect } from 'react'
import { createSession, isInvalidSessionError, sendMessage } from '../services/api'
import ExportButton from './ExportButton'

const SUGGESTIONS = [
  'Como faço para me associar?',
  'Qual o endereço da AMOBEM?',
  'Quais os próximos eventos?',
]

const SESSION_STORAGE_KEY = 'amoben.chat.session.v1'

function sessionExpiration(session) {
  const expiresAt = Date.parse(session?.expires_at)
  return Number.isNaN(expiresAt) ? 0 : expiresAt
}

function isSessionValid(session) {
  return Boolean(
    session
    && typeof session.session_id === 'string'
    && session.session_id
    && typeof session.session_token === 'string'
    && session.session_token
    && sessionExpiration(session) > Date.now()
  )
}

function clearStoredSession(expectedToken) {
  try {
    const raw = sessionStorage.getItem(SESSION_STORAGE_KEY)
    if (!raw) return

    const stored = JSON.parse(raw)
    if (!expectedToken || stored?.session_token === expectedToken) {
      sessionStorage.removeItem(SESSION_STORAGE_KEY)
    }
  } catch {
    try {
      sessionStorage.removeItem(SESSION_STORAGE_KEY)
    } catch {
      // O chat ainda pode funcionar mesmo se o navegador bloquear o storage.
    }
  }
}

function loadStoredSession() {
  try {
    const session = JSON.parse(sessionStorage.getItem(SESSION_STORAGE_KEY))
    if (isSessionValid(session)) return session
  } catch {
    // Uma entrada ausente ou malformada será simplesmente descartada.
  }

  clearStoredSession()
  return null
}

function storeSession(session) {
  try {
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))
  } catch {
    // Mantém a sessão apenas em memória quando o storage não está disponível.
  }
}

function now() {
  return new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
}

function mapCoordinates(value) {
  try {
    const url = new URL(value)
    let latitude
    let longitude

    if (['openstreetmap.org', 'www.openstreetmap.org'].includes(url.hostname)) {
      latitude = Number(url.searchParams.get('mlat'))
      longitude = Number(url.searchParams.get('mlon'))
    } else if (
      ['google.com', 'www.google.com', 'maps.google.com'].includes(url.hostname)
      && url.pathname.startsWith('/maps/search')
    ) {
      const [rawLatitude, rawLongitude] = (url.searchParams.get('query') || '').split(',')
      latitude = Number(rawLatitude)
      longitude = Number(rawLongitude)
    } else {
      return null
    }

    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null

    return { latitude, longitude }
  } catch {
    return null
  }
}

function mapPreviewUrl(value) {
  const coordinates = mapCoordinates(value)
  if (!coordinates) return null

  const { latitude, longitude } = coordinates
  try {
    const latitudeOffset = 0.0025
    const longitudeOffset = 0.0045
    const params = new URLSearchParams({
      bbox: [
        longitude - longitudeOffset,
        latitude - latitudeOffset,
        longitude + longitudeOffset,
        latitude + latitudeOffset,
      ].join(','),
      layer: 'mapnik',
      marker: `${latitude},${longitude}`,
    })
    return `https://www.openstreetmap.org/export/embed.html?${params}`
  } catch {
    return null
  }
}

function MapPreview({ url, index }) {
  const previewUrl = mapPreviewUrl(url)
  if (!previewUrl) return null

  const coordinates = mapCoordinates(url)
  const googleMapsUrl = coordinates
    ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${coordinates.latitude},${coordinates.longitude}`)}`
    : url

  return (
    <span className="map-preview">
      <iframe
        src={previewUrl}
        title={`Prévia da localização ${index + 1}`}
        loading="lazy"
        referrerPolicy="no-referrer"
        sandbox="allow-scripts allow-same-origin"
        tabIndex="-1"
        aria-hidden="true"
      />
      <a
        className="map-preview-open"
        href={googleMapsUrl}
        target="_blank"
        rel="noreferrer"
        aria-label="Abrir esta localização no Google Maps"
      >
        <span>⌖ Abrir no Google Maps</span>
      </a>
      <a
        className="map-preview-attribution"
        href="https://www.openstreetmap.org/copyright"
        target="_blank"
        rel="noreferrer"
      >
        © OpenStreetMap
      </a>
    </span>
  )
}

function MessageContent({ content }) {
  return content.split(/(https?:\/\/[^\s]+)/g).map((part, index) => {
    if (!part.startsWith('http://') && !part.startsWith('https://')) return part

    const previewUrl = mapPreviewUrl(part)
    if (previewUrl) {
      return <MapPreview key={`${part}-${index}`} url={part} index={index} />
    }

    return (
      <a key={`${part}-${index}`} href={part} target="_blank" rel="noreferrer">
        Abrir link
      </a>
    )
  })
}

export default function Chat() {
  const [open, setOpen]         = useState(false)
  const [messages, setMessages] = useState([])
  const [input, setInput]       = useState('')
  const [loading, setLoading]   = useState(false)
  const [session, setSession]   = useState(loadStoredSession)
  const bottomRef               = useRef(null)
  const inputRef                = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, open])

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  useEffect(() => {
    if (!session) return undefined

    let expirationTimer

    function scheduleExpiration() {
      const remaining = sessionExpiration(session) - Date.now()
      if (remaining <= 0) {
        clearStoredSession(session.session_token)
        setSession(current => (
          current?.session_token === session.session_token ? null : current
        ))
        return
      }

      expirationTimer = window.setTimeout(
        scheduleExpiration,
        Math.min(remaining, 2_147_483_647),
      )
    }

    scheduleExpiration()
    return () => window.clearTimeout(expirationTimer)
  }, [session])

  function invalidateSession(sessionToken) {
    clearStoredSession(sessionToken)
    setSession(current => (
      !sessionToken || current?.session_token === sessionToken ? null : current
    ))
  }

  async function createAndStoreSession() {
    const newSession = await createSession()
    if (!isSessionValid(newSession)) {
      throw new Error('O servidor retornou uma sessão inválida.')
    }

    storeSession(newSession)
    setSession(newSession)
    return newSession
  }

  async function getValidSession() {
    if (isSessionValid(session)) return session

    if (session) invalidateSession(session.session_token)
    return createAndStoreSession()
  }

  async function sendWithValidSession(text) {
    let activeSession = await getValidSession()

    try {
      return await sendMessage(
        activeSession.session_id,
        activeSession.session_token,
        text,
      )
    } catch (error) {
      if (!isInvalidSessionError(error)) throw error
      invalidateSession(activeSession.session_token)
    }

    activeSession = await createAndStoreSession()

    try {
      return await sendMessage(
        activeSession.session_id,
        activeSession.session_token,
        text,
      )
    } catch (error) {
      if (isInvalidSessionError(error)) {
        invalidateSession(activeSession.session_token)
      }
      throw error
    }
  }

  async function send(text) {
    text = text.trim()
    if (!text || loading) return

    setMessages(prev => [...prev, { role: 'user', content: text, ts: now() }])
    setInput('')
    setLoading(true)
    setMessages(prev => [...prev, { role: 'bot', content: '', ts: '', id: 'typing' }])

    try {
      const data = await sendWithValidSession(text)
      setMessages(prev =>
        prev.map(m => m.id === 'typing'
          ? { role: 'bot', content: data.response, ts: now() }
          : m
        )
      )
    } catch (err) {
      setMessages(prev =>
        prev.map(m => m.id === 'typing'
          ? { role: 'error', content: 'Não foi possível enviar a mensagem. Tente novamente.', ts: '' }
          : m
        )
      )
    } finally {
      setLoading(false)
    }
  }

  function handleSubmit(e) {
    e.preventDefault()
    send(input)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send(input)
    }
  }

  if (!open) {
    return (
      <button
        className="chat-launcher launcher-pulse"
        onClick={() => setOpen(true)}
        aria-label="Abrir assistente virtual"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
        </svg>
      </button>
    )
  }

  return (
    <div className="chat-window widget-in">
      <header className="chat-header">
        <div className="chat-header-avatar">AM</div>
        <div className="chat-header-info">
          <div className="chat-title">Assistente AMOBEN</div>
          <div className="chat-status"><span className="status-dot" /> Online · responde em segundos</div>
        </div>
        <ExportButton
          sessionId={session?.session_id}
          sessionToken={session?.session_token}
          sessionExpiresAt={session?.expires_at}
          messages={messages}
          onSessionInvalid={invalidateSession}
        />
        <button className="chat-close" onClick={() => setOpen(false)} aria-label="Fechar">×</button>
      </header>

      <div className="chat-messages">
        {messages.length === 0 ? (
          <div className="chat-empty">
            <span className="chat-empty-icon">👋</span>
            Olá! Sou o assistente virtual da AMOBEN.<br />
            Como posso ajudar você hoje?
            <div className="chat-suggestions">
              {SUGGESTIONS.map(s => (
                <button key={s} className="chip" onClick={() => send(s)}>{s}</button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={`bubble-wrap ${m.role} msg-in`}>
              <div className={`bubble ${m.role}`}>
                {m.id === 'typing'
                  ? <><span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" /></>
                  : <MessageContent content={m.content} />}
              </div>
              {m.ts && <div className="bubble-meta">{m.ts}</div>}
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      <form className="chat-form" onSubmit={handleSubmit}>
        <textarea
          ref={inputRef}
          className="chat-input"
          rows={1}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Digite sua mensagem..."
          disabled={loading}
        />
        <button className="chat-send" type="submit" disabled={loading || !input.trim()} aria-label="Enviar">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="22" y1="2" x2="11" y2="13" />
            <polygon points="22 2 15 22 11 13 2 9 22 2" />
          </svg>
        </button>
      </form>
    </div>
  )
}

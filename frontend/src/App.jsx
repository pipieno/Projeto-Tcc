import Chat from './components/Chat'

export default function App() {
  return (
    <div className="page">
      {/* Navbar */}
      <header className="topbar">
        <nav className="nav">
          <div className="brand">
            <span className="logo">AM</span>
            AMOBEN
          </div>
          <div className="nav-links">
            <a href="#historia">História</a>
            <a href="#noticias">Notícias</a>
            <a href="#contato">Contato</a>
          </div>
        </nav>
      </header>

      {/* Hero */}
      <section className="hero">
        <div className="hero-inner">
          <h1>AMOBEN</h1>
          <p className="hero-sub">Associação de Moradores do Bairro Estrada Nova</p>
          <p className="hero-meta">Jaraguá do Sul · SC · desde 5 de fevereiro de 2001</p>
        </div>
      </section>

      {/* Nossa história */}
      <section id="historia" className="section split">
        <div>
          <span className="eyebrow">A Associação</span>
          <h2>Nossa história</h2>
          <p>
            Fundada em 5 de fevereiro de 2001, a AMOBEN representa e defende os interesses dos
            moradores do bairro Estrada Nova. A associação atua em diversas frentes, como a defesa
            dos direitos sociais e a promoção de atividades culturais e esportivas.
          </p>
          <p>
            Com sede na Rua José Picolli, nº 532, a entidade mantém um canal próximo com a
            comunidade e dialoga com o poder público em busca de melhorias para o bairro.
          </p>
        </div>
        <div className="figure">Associação de Moradores<br />do Bairro Estrada Nova</div>
      </section>

      {/* Bairro Estrada Nova */}
      <section className="split alt">
        <div className="section split" style={{ padding: 0, maxWidth: '100%' }}>
          <div className="figure teal">Bairro Estrada Nova<br />Jaraguá do Sul · SC</div>
          <div>
            <span className="eyebrow">O Bairro</span>
            <h2>Bairro Estrada Nova</h2>
            <p>
              A estrada que deu nome ao bairro foi aberta em 1920, e a região cresceu com a chegada
              das primeiras famílias de colonos no início do século XX.
            </p>
            <p>
              Ao longo das décadas, o que antes era uma área predominantemente rural passou por um
              intenso processo de urbanização a partir dos anos 1980, dando origem ao bairro que
              conhecemos hoje.
            </p>
          </div>
        </div>
      </section>

      {/* App em breve */}
      <section className="section app-soon">
        <span className="eyebrow" style={{ color: '#5eead4' }}>Novidade</span>
        <h2>Em breve teremos nosso App</h2>
        <div className="cards">
          <div className="card">
            <div className="card-icon">📰</div>
            <p>Fique por dentro das novidades!</p>
          </div>
          <div className="card">
            <div className="card-icon">📅</div>
            <p>Acompanhe os eventos e atividades locais!</p>
          </div>
          <div className="card">
            <div className="card-icon">🗳️</div>
            <p>Participe de enquetes e pesquisas!</p>
          </div>
        </div>
      </section>

      {/* Notícias */}
      <section id="noticias" className="section">
        <span className="eyebrow">Comunidade</span>
        <h2 style={{ marginBottom: 28 }}>Notícias</h2>
        <div className="news-grid">
          <article className="news-card">
            <div className="news-cover">Estrada Nova</div>
            <div className="news-body">
              <span className="news-date">Outubro de 2024</span>
              <h3 className="news-title">
                Três ruas no bairro Estrada Nova recebem pavimentação asfáltica
              </h3>
              <p>
                As obras de pavimentação contemplaram cerca de 170 metros de extensão, com um
                investimento de R$ 446.043,50. A melhoria atende a uma antiga demanda dos moradores
                e traz mais qualidade de vida e mobilidade para o bairro.
              </p>
            </div>
          </article>
          <aside className="news-aside">
            <div className="news-mini">
              <span className="news-date">Em breve</span>
              <strong>Assembleia geral de moradores</strong>
            </div>
            <div className="news-mini">
              <span className="news-date">Em breve</span>
              <strong>Mutirão de limpeza e arborização</strong>
            </div>
          </aside>
        </div>
      </section>

      {/* Footer / Fale Conosco */}
      <footer id="contato" className="footer">
        <div className="footer-top">
          <div>
            <h3>Fale Conosco</h3>
            <p>
              Tem alguma dúvida sobre a associação ou o bairro? Use o assistente virtual no canto
              da tela ou entre em contato pelos canais ao lado.
            </p>
          </div>
          <div>
            <h4>Contato</h4>
            <p>
              <a href="mailto:amoben@contabilidadedalagnese.com">amoben@contabilidadedalagnese.com</a><br />
              (47) 3370-8284
            </p>
          </div>
          <div>
            <h4>Endereço</h4>
            <p>
              Rua José Picolli, nº 532, Box 23<br />
              Jaraguá do Sul · SC<br />
              CEP 89254-350
            </p>
          </div>
        </div>
        <div className="footer-bottom">© copyright 2024 todos os direitos reservados · AMOBEN</div>
      </footer>

      <Chat />
    </div>
  )
}

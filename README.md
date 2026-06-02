# Agente Conversacional com Stack de Observabilidade

Trabalho de Conclusão de Curso — Implementação de um agente conversacional baseado em LLM local com uma stack completa de observabilidade (logs, métricas e rastreamento distribuído).

---

## Visão Geral

O projeto consiste em um assistente virtual da **AMOBEM** (Associação de Moradores do Bairro Estrada Nova), restrito a responder apenas perguntas sobre a associação e o bairro. Toda a infraestrutura é instrumentada com as três pilares de observabilidade:

- **Logs estruturados** — JSON via `python-json-logger` → Promtail → Loki
- **Métricas** — `prometheus-client` exposto no FastAPI → Prometheus → Grafana
- **Rastreamento distribuído** — OpenTelemetry SDK → Jaeger via OTLP/gRPC

---

## Arquitetura

```
┌─────────────────────────────────────────────────────┐
│                   Cliente (Browser)                  │
│              http://localhost:3001                   │
└──────────────────────┬──────────────────────────────┘
                       │
              ┌────────▼────────┐
              │    Frontend     │  React + Vite → nginx
              │   (porta 3001)  │
              └────────┬────────┘
                       │ /chat  /export
              ┌────────▼────────┐
              │     Backend     │  FastAPI + LangChain + Ollama
              │   (porta 8080)  │  gemma3:4b (host)
              └──┬──────┬───┬───┘
                 │      │   │
         logs/  │   métricas  traces
         .log   │   /metrics  OTLP gRPC
                 │      │   │
    ┌────────────▼┐  ┌───▼───▼──┐  ┌────────┐
    │  Promtail   │  │Prometheus │  │ Jaeger │
    └──────┬──────┘  └────┬──────┘  │(16686) │
           │              │         └────────┘
    ┌──────▼──────┐  ┌────▼──────────────────┐
    │    Loki     │  │        Grafana         │
    │  (3100)     │  │       (3000)           │
    └─────────────┘  └───────────────────────┘
```

---

## Pré-requisitos

- [Docker](https://docs.docker.com/get-docker/) e Docker Compose
- [Ollama](https://ollama.com) instalado e rodando no host com o modelo `gemma3:4b`

```bash
# instalar o modelo
ollama pull gemma3:4b
```

O Ollama precisa escutar em `0.0.0.0` para ser acessível pelo container:

```bash
# criar override do systemd
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
EOF

sudo systemctl daemon-reload
sudo systemctl restart ollama
```

---

## Como rodar

```bash
# clonar o repositório
git clone <url-do-repo>
cd Projeto-Tcc

# subir toda a stack
docker compose up -d --build
```

Na primeira vez o Docker vai baixar as imagens e compilar o frontend React — pode demorar alguns minutos.

---

## Serviços e portas

| Serviço | URL | Descrição |
|---|---|---|
| Frontend | http://localhost:3001 | Chat com o agente |
| Backend API | http://localhost:8080 | FastAPI (métricas em `/metrics`) |
| Grafana | http://localhost:3000 | Dashboards (admin / admin) |
| Prometheus | http://localhost:9091 | Métricas brutas |
| Jaeger | http://localhost:16686 | Rastreamento distribuído |
| Loki | http://localhost:3100 | API de logs (sem UI) |

---

## Dashboard de Observabilidade

O dashboard **AMOBEM — Agente Conversacional** é provisionado automaticamente no Grafana ao iniciar a stack.

![Dashboard Grafana](image.png)

**Painéis disponíveis:**

| Seção | Painéis |
|---|---|
| Visão Geral | Total de interações · Taxa de erro · Sessões ativas · Latência P50 |
| Performance do Agente | Latência P50/P90/P99 ao longo do tempo · Tempo médio por modelo |
| Volume de Uso | Interações por minuto · Sucesso vs Erro |
| Recursos do Sistema | CPU % · Memória RAM % |
| Logs | Distribuição por nível (donut) · Erros recentes |

---

## Estrutura do projeto

```
Projeto-Tcc/
├── backend/
│   ├── app.py              # FastAPI + LangChain + observabilidade
│   ├── system_prompt.txt   # Prompt do agente AMOBEM
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── Chat.jsx        # Interface do chat
│   │   │   └── ExportButton.jsx # Exportação JSON/CSV
│   │   └── services/api.js     # Comunicação com o backend
│   ├── nginx.conf
│   └── Dockerfile
├── observability/
│   ├── prometheus.yml
│   ├── loki-config.yml
│   ├── promtail-config.yml
│   └── grafana/
│       ├── provisioning/
│       │   ├── datasources/    # Prometheus, Loki, Jaeger
│       │   └── dashboards/
│       └── dashboards/
│           └── amobem.json     # Dashboard provisionado
├── logs/                       # Logs gerados pelo agente (volume)
└── docker-compose.yml
```

---

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Endpoint do Ollama |
| `OLLAMA_MODEL` | `gemma3:4b` | Modelo de linguagem utilizado |
| `JAEGER_ENDPOINT` | `http://jaeger:4317` | Endpoint OTLP do Jaeger |

---

## Exportação de conversas

O frontend permite exportar o histórico de qualquer sessão em dois formatos:

- **JSON** — estrutura completa com timestamps e metadados
- **CSV** — formato tabular com `role, timestamp, content`

Os botões aparecem no cabeçalho do chat após a primeira mensagem enviada.

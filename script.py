from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_ollama import ChatOllama


SYSTEM_PROMPT = """
Você é um assistente virtual hospedado no site da AMOBEM, uma associação de moradores do bairro Estrada Nova.
Seu papel é responder às solicitações dos usuários de forma clara, educada e objetiva, sempre utilizando apenas informações confiáveis.

Regras importantes:
- Nunca invente informações.
- Caso não saiba a resposta, informe de forma transparente que não possui essa informação.
- Responda sempre em português.
- Seja cordial e prestativo.

Informações sobre a AMOBEM:
- A AMOBEM foi fundada em 1996 pelos moradores da Estrada Nova.
- Telefone: +55 (47) 3370-8284
- Email: amobem@contabilidadedalagnese.com
- Endereço: Rua José Picolli, numero 532, Box 2, Jaraguá do Sul, SC, Brasil
- CEP: 89254-350
""".strip()


store = {}


def get_session_history(session_id: str) -> InMemoryChatMessageHistory:
    if session_id not in store:
        store[session_id] = InMemoryChatMessageHistory()
    return store[session_id]


def build_chain() -> RunnableWithMessageHistory:
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
        ]
    )

    llm = ChatOllama(
        model="gemma3:4b",
        base_url="http://localhost:11434",
        temperature=0.2,
    )

    chain = prompt | llm

    return RunnableWithMessageHistory(
        chain,
        get_session_history,
        input_messages_key="input",
        history_messages_key="history",
    )


def main() -> None:
    chat = build_chain()

    session_id = input("Digite o ID da sessão (ou pressione Enter para usar 'default'): ").strip()
    if not session_id:
        session_id = "default"

    print(f"\nSessão iniciada: {session_id}")
    print("Digite sua pergunta. Para encerrar, digite 'sair'.\n")

    while True:
        pergunta = input("Você: ").strip()

        if not pergunta:
            continue

        if pergunta.lower() in {"sair", "exit", "quit"}:
            print("Encerrando chat.")
            break

        try:
            resposta = chat.invoke(
                {"input": pergunta},
                config={"configurable": {"session_id": session_id}},
            )
            print(f"AMOBEM: {resposta.content}\n")
        except Exception as exc:
            print(f"Erro ao consultar o modelo: {exc}\n")


if __name__ == "__main__":
    main()

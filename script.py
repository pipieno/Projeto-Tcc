import os

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_ollama import ChatOllama


def load_system_prompt(path: str = "system_prompt.txt") -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


SYSTEM_PROMPT = load_system_prompt()


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
        model=os.getenv("OLLAMA_MODEL", "gemma3:4b"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
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
        except (ConnectionError, TimeoutError, OSError) as exc:
            print(f"Erro de conexão com o modelo: {exc}\n")
        except Exception as exc:
            print(f"Erro inesperado: {exc}\n")
            raise


if __name__ == "__main__":
    main()

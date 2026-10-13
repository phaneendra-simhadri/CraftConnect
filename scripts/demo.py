"""Drive one full listing through the agent without the web UI.

Run:  python -m scripts.demo
"""

from backend import agent, interview
from backend.sessions import store

ANSWERS = [q["quick_replies"][0] for q in interview.QUESTIONS]


def show(reply):
    for message in reply["messages"]:
        print("BOT:", message.replace("\n", "\n     "))
    if reply["listing"]:
        removed = reply["listing"]["removed"]
        print(f"     [guard] blocked {len(removed)} unverified claim(s)")
    print()


def main():
    session = store.get()
    show(agent.handle_message(session, "hi"))

    for answer in ANSWERS:
        show(agent.handle_message(session, answer))
        show(agent.handle_message(session, "Yes"))

    show(agent.handle_message(session, "publish"))
    print("Buyer link:", f"/buyer/{session.id}")


if __name__ == "__main__":
    main()

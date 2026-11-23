"""`concierge` command-line entry point.

    python -m scripts.cli chat            # talk to the assistant in a terminal (in-process stack)
    python -m scripts.cli seed            # create demo bookings and print their references
    python -m scripts.cli serve --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import os


async def _chat(channel: str) -> None:
    from agents.runtime import ConciergeRuntime
    from common.messages import InboundMessage

    rt = await ConciergeRuntime().start()
    print(f"Concierge ({rt.mode} mode). Type 'quit' to exit, '/approve' or '/reject' to act on a pending approval.\n")
    session = f"{channel}-terminal"
    try:
        while True:
            text = await asyncio.to_thread(input, "you> ")
            if text.strip().lower() in ("quit", "exit"):
                break
            if text.startswith(("/approve", "/reject")):
                pending = await rt.hitl.list_approvals("pending")
                if not pending:
                    print("(no pending approvals)")
                    continue
                out = await rt.resolve_approval(pending[0]["id"], text.startswith("/approve"), "terminal.operator", None)
                print(f"bot> {out.text}\n")
                continue
            out = await rt.handle(InboundMessage(channel=channel, address="terminal", text=text, session_id=session))
            tag = f"[{out.status} | {'+'.join(out.agents) or 'router'} | {out.mode} | {out.latency_ms:.0f} ms]"
            print(f"bot> {out.text}\n     {tag}")
            for i, c in enumerate(out.citations, 1):
                print(f"     [{i}] {c['title']}: {c['section']}")
            print()
    finally:
        await rt.stop()


async def _seed() -> None:
    from services.client import BackendClient

    client = BackendClient()
    demo = [
        ("DLX", "2026-12-12", "2026-12-15", 2, "Alex Morgan", "alex@example.com"),
        ("PEN", "2027-02-10", "2027-02-12", 2, "Priya Natarajan", "priya@example.com"),
        ("STD", "2027-01-08", "2027-01-10", 1, "Tomas Herrera", "tomas@example.com"),
    ]
    for room, ci, co, guests, name, email in demo:
        quote = await client.quote(room_type=room, check_in=ci, check_out=co, guests=guests, rooms=1)
        res = await client.create_reservation({"quote_id": quote.data["quote_id"], "guest_name": name, "guest_email": email}, None)
        print(f"{res.data.get('reservation_id', res.status):12} {room} {ci} -> {co}  {email}  total {quote.data['total']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="concierge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    chat = sub.add_parser("chat")
    chat.add_argument("--channel", default="web", choices=["web", "whatsapp", "email", "voice"])
    sub.add_parser("seed")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    from common.logging import setup_logging

    setup_logging()
    if args.cmd == "chat":
        asyncio.run(_chat(args.channel))
    elif args.cmd == "seed":
        asyncio.run(_seed())
    else:
        import uvicorn

        uvicorn.run("channels.gateway:app", host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()

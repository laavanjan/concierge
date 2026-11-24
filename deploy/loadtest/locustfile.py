"""Load test: a mix of policy questions and full booking conversations against /v1/chat.

    pip install .[loadtest]
    locust -f deploy/loadtest/locustfile.py --headless -u 20 -r 5 -t 1m --host http://localhost:8000
"""

from __future__ import annotations

import random
import uuid

from locust import HttpUser, between, task

QUESTIONS = [
    "Can I bring my dog?", "What time is check-in?", "Is breakfast included?", "How much is parking?",
    "What's the cancellation policy?", "Do kids stay free?",
]


class Guest(HttpUser):
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.session = f"web-load-{uuid.uuid4().hex[:10]}"

    def say(self, text: str, name: str) -> dict:
        with self.client.post("/v1/chat", json={"text": text, "session_id": self.session}, name=name, catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
                return {}
            return r.json()

    @task(3)
    def policy_question(self) -> None:
        self.say(random.choice(QUESTIONS), "policy")

    @task(1)
    def booking_conversation(self) -> None:
        self.session = f"web-load-{uuid.uuid4().hex[:10]}"
        day = random.randint(1, 25)
        self.say(f"book {day}-{day + 2} Jan for 2", "booking: search")
        self.say("the standard queen", "booking: quote")
        self.say(f"yes, Load Tester, load{uuid.uuid4().hex[:6]}@example.com", "booking: confirm")

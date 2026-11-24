PY ?= python

.PHONY: install dev test lint eval gate eval-live compare chat seed up down helm-lint load pdf golden

install:            ## install with dev extras
	$(PY) -m pip install -e ".[dev]"

dev:                ## run everything in one process on :8000 (services + MCP in-process)
	$(PY) -m uvicorn channels.gateway:app --reload --port 8000

test:               ## unit, integration and LLM-path tests
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

eval:               ## offline eval suites + quality gate (what CI runs)
	$(PY) -m evals.run --suite all --mode offline --out evals/reports/offline-latest.json
	$(PY) -m evals.gate evals/reports/offline-latest.json --baseline evals/reports/baseline.json

eval-live:          ## live Claude eval (needs ANTHROPIC_API_KEY; costs money)
	$(PY) -m evals.run --suite all --mode live --limit 30 --out evals/reports/live-latest.json

compare:            ## before/after table, e.g. make compare A=evals/reports/router-opus.json B=evals/reports/router-haiku.json
	$(PY) -m evals.compare $(A) $(B)

golden:             ## regenerate the golden conversation set
	$(PY) -m evals.generate_golden

chat:               ## talk to the assistant in the terminal
	$(PY) -m scripts.cli chat

seed:               ## create demo bookings and print their references
	$(PY) -m scripts.cli seed

up:                 ## full stack with Postgres, Redis, Qdrant, Phoenix
	docker compose up --build -d

down:
	docker compose down -v

helm-lint:
	helm lint deploy/helm/concierge

load:               ## 1-minute load test against a running gateway
	locust -f deploy/loadtest/locustfile.py --headless -u 20 -r 5 -t 1m --host http://localhost:8000

pdf:                ## regenerate the sample policy PDF
	$(PY) -m scripts.make_policy_pdf

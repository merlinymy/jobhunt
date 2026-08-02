PY := .venv/bin/python

.DEFAULT_GOAL := help
.PHONY: help venv migrate load-profile dev ingest score digest tailor inbox chat

help:  ## list targets
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sort | awk -F':.*?## ' '{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

$(PY):
	uv venv --python 3.12
	uv pip install -e .

venv: $(PY)  ## create .venv and install the package

migrate: $(PY)  ## apply migrations/*.sql in order
	$(PY) -m jobhunt.migrate

load-profile: $(PY)  ## import docs/profile/* into SQLite; idempotent, re-runnable
	$(PY) -m jobhunt.load_profile

dev: $(PY)  ## dashboard on localhost:8000
	$(PY) -m jobhunt.web.app

ingest: $(PY)  ## one-shot discovery run
	$(PY) -m jobhunt.ingest

score: $(PY)  ## prefilter + LLM scoring on `discovered`
	$(PY) -m jobhunt.score

digest: $(PY)  ## send today's Telegram digest
	$(PY) -m jobhunt.digest

tailor: $(PY)  ## tailor against one JD: make tailor JD=path/to/jd.txt  (packets: Phase 3)
	$(PY) -m jobhunt.tailor $(JD)

inbox: $(PY)  ## poll Gmail, classify, update states
	$(PY) -m jobhunt.inbox

chat: $(PY)  ## gap-filling: resolve unknown_questions, append answers
	$(PY) -m jobhunt.chat

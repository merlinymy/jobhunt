PY := .venv/bin/python

.DEFAULT_GOAL := help
.PHONY: help venv migrate load-profile dev ingest score tailor inbox chat \
        install-agents agents-stop doctor backup test

help:  ## list targets
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sort | awk -F':.*?## ' '{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

$(PY):
	uv venv --python 3.12
	# With the extras. anthropic, jobspy and rendercv are imported lazily inside
	# functions, so a bare install passes every check and then dies at 06:30.
	uv pip install -e '.[llm,resume,ingest]'

venv: $(PY)  ## create .venv and install the package

migrate: $(PY)  ## apply migrations/*.sql in order
	$(PY) -m jobhunt.migrate

load-profile: $(PY)  ## import docs/profile/* into SQLite; idempotent, re-runnable
	$(PY) -m jobhunt.load_profile

dev: $(PY)  ## dashboard on localhost:8000, with auto-reload
	$(PY) -m jobhunt.web.app --reload

ingest: $(PY)  ## one-shot discovery run (ARGS=--boards-only to skip the scrape)
	$(PY) -m jobhunt.ingest $(ARGS)

score: $(PY)  ## prefilter + LLM scoring on `discovered`
	$(PY) -m jobhunt.score $(ARGS)

tailor: $(PY)  ## tailor against one JD: make tailor JD=path/to/jd.txt  (packets: Phase 3)
	$(PY) -m jobhunt.tailor $(JD)

install-agents:  ## install the launchd agents — MINI ONLY, see deploy/install.sh
	./deploy/install.sh

agents-stop:  ## stop the agents (do this before ejecting the disk)
	@for l in discover dashboard backup; do \
	  launchctl bootout gui/$$(id -u)/com.jobhunt.$$l 2>/dev/null && echo "  stopped com.jobhunt.$$l" || true; \
	done

doctor: $(PY)  ## check the deployment: disk, schema, backups, extras, bind
	$(PY) -m jobhunt.doctor

backup: $(PY)  ## snapshot the DB to the internal disk, verify, prune
	$(PY) -m jobhunt.backup $(ARGS)

test: $(PY)  ## the table-driven suites (standalone scripts, not pytest)
	$(PY) tests/test_url_normalization.py
	$(PY) tests/test_tailoring_validator.py

inbox: $(PY)  ## poll Gmail, classify, update states
	$(PY) -m jobhunt.inbox

chat: $(PY)  ## gap-filling: resolve unknown_questions, append answers
	$(PY) -m jobhunt.chat

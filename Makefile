PY := .venv/bin/python

.DEFAULT_GOAL := help
.PHONY: help venv migrate load-profile dev ingest score tailor inbox chat \
        install-agents agents-stop doctor backup test \
        build-web dev-web check-web profile-push profile-pull test-live

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

# docs/profile/ used to sync between machines through git. It no longer does —
# it holds a legal name, a phone number, and other people's contact details — so
# these move it directly over the tailnet instead. HOST defaults to the mini.
HOST ?= jobhunt-mini
REMOTE_PROFILE ?= projects/jobhunt/docs/profile/

profile-push:  ## copy docs/profile/ to the host (HOST=... to override)
	rsync -av --exclude='.DS_Store' docs/profile/ "$(HOST):$(REMOTE_PROFILE)"
	@echo "  now run 'make load-profile' on $(HOST)"

profile-pull:  ## copy docs/profile/ back from the host
	rsync -av --exclude='.DS_Store' "$(HOST):$(REMOTE_PROFILE)" docs/profile/

doctor: $(PY)  ## check the deployment: disk, schema, backups, extras, bind
	$(PY) -m jobhunt.doctor

backup: $(PY)  ## snapshot the DB to the internal disk, verify, prune
	$(PY) -m jobhunt.backup $(ARGS)

build-web:  ## compile the React app into jobhunt/web/dist
	cd web && npm ci && npm run build

dev-web:  ## Vite on 5173 proxying /api to 8000 — run alongside `make dev`
	cd web && npm run dev

check-web:  ## eslint + tsc on the frontend
	cd web && npm run lint && npm run typecheck

test: $(PY)  ## the table-driven suites (standalone scripts, not pytest)
	$(PY) tests/test_url_normalization.py
	$(PY) tests/test_tailoring_validator.py
	$(PY) tests/test_select_engine.py

test-live: $(PY)  ## score the model checker against the adversarial fixtures (costs cents)
	$(PY) tests/test_tailoring_validator.py --live

inbox: $(PY)  ## poll Gmail, classify, update states
	$(PY) -m jobhunt.inbox

chat: $(PY)  ## gap-filling: resolve unknown_questions, append answers (ARGS=--list)
	$(PY) -m jobhunt.chat $(ARGS)

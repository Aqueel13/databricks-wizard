COMPOSE := docker compose -f docker/compose.yml
SRC     := uv run --project sources -q wizard-sources

.PHONY: help data data-clean up seed cdc api sources simulate status down reset dbx-setup deploy ship ship-loop ground-truth ingest

help:          ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

data:          ## download all external datasets into data/raw (idempotent)
	bash scripts/fetch_data.sh

data-clean:    ## delete downloaded data
	rm -rf data/raw

up:            ## start Postgres, Redpanda and the console
	$(COMPOSE) up -d postgres redpanda console

seed:          ## build universe + ground truth, load SAP and Coupa, write HICX exports and documents
	$(SRC) seed

cdc:           ## start Debezium and register the SAP CDC connector (run after seed)
	$(COMPOSE) --profile cdc up -d

api:           ## build and start the Coupa mock API on :8081
	$(COMPOSE) --profile api up -d --build coupa-api

sources: up seed cdc api   ## everything for M1, in order

simulate:      ## keep producing live changes (Ctrl-C to stop)
	$(SRC) simulate --interval 5

status:        ## containers, connector state, CDC topic sizes
	@$(COMPOSE) --profile cdc --profile api ps --format '{{.Service}}: {{.Status}}'
	@curl -s localhost:8083/connectors/sap-cdc/status | python3 -c "import json,sys; d=json.load(sys.stdin); print('sap-cdc:', d['connector']['state'], [t['state'] for t in d['tasks']])" 2>/dev/null || echo "sap-cdc: not running"
	@$(COMPOSE) exec -T redpanda rpk topic list 2>/dev/null | grep '^erp' || true

down:          ## stop all containers (keeps data volumes)
	$(COMPOSE) --profile cdc --profile api down

reset:         ## DESTRUCTIVE: stop everything and delete Postgres/Redpanda volumes (re-run `make sources`)
	$(COMPOSE) --profile cdc --profile api down -v

# ---------------------------------------------------------------- M2: landing + bronze
SHIP := uv run --project shipper -q wizard-shipper

dbx-setup:     ## create catalog, schemas and volumes in the workspace (idempotent)
	bash scripts/dbx_setup.sh

deploy:        ## deploy the bundle (bronze pipeline + file-triggered ingest job)
	databricks bundle deploy

ship:          ## ship every source to the landing volume once
	$(SHIP) run

ship-loop:     ## keep shipping every 30 s (with `make simulate` = continuous flow)
	$(SHIP) run --interval 30

ground-truth:  ## upload the generator's answer key to the ops volume
	$(SHIP) ground-truth

ingest:        ## run the bronze pipeline now (normally the file-arrival trigger does this)
	databricks bundle run wizard_ingest

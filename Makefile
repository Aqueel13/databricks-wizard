.PHONY: data data-clean

data:          ## download all external datasets into data/raw (idempotent)
	bash scripts/fetch_data.sh

data-clean:    ## delete downloaded data
	rm -rf data/raw

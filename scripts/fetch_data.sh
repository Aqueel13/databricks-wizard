#!/usr/bin/env bash
# Download all external datasets into data/raw/<source>/.
# Idempotent: a source is skipped once its .done marker exists (delete it to re-fetch).
# Needs: kaggle CLI with ~/.kaggle/kaggle.json, curl, unzip.
# Licences and roles: docs/data-model.md#external-datasets
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RAW="$ROOT/data/raw"
mkdir -p "$RAW"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# kaggle_files <source> <dataset-ref> <file>...   (selected files only)
kaggle_files() {
  local src=$1 ref=$2; shift 2
  local dir="$RAW/$src"
  [[ -f "$dir/.done" ]] && { log "skip $src (done)"; return; }
  mkdir -p "$dir"
  for f in "$@"; do
    local base; base=$(basename "$f")
    [[ -f "$dir/$base" ]] && continue
    log "$src: $f"
    kaggle datasets download -q -p "$dir" -f "$f" "$ref" >/dev/null
    if [[ -f "$dir/$base.zip" ]]; then unzip -oq "$dir/$base.zip" -d "$dir" && rm "$dir/$base.zip"; fi
  done
  # some files unzip into their original sub-path; flatten them
  find "$dir" -mindepth 2 -type f -exec mv -n {} "$dir/" \; ; find "$dir" -mindepth 1 -type d -empty -delete
  touch "$dir/.done"
}

# kaggle_all <source> <dataset-ref>   (whole dataset)
kaggle_all() {
  local src=$1 ref=$2 dir="$RAW/$1"
  [[ -f "$dir/.done" ]] && { log "skip $src (done)"; return; }
  mkdir -p "$dir"
  log "$src: whole dataset $ref"
  kaggle datasets download -q -p "$dir" --unzip "$ref" >/dev/null
  touch "$dir/.done"
}

# url <source> <url> <filename>
url() {
  local src=$1 u=$2 name=$3 dir="$RAW/$1"
  [[ -f "$dir/.done" ]] && { log "skip $src (done)"; return; }
  mkdir -p "$dir"
  log "$src: $u"
  curl -fsSL -o "$dir/$name" "$u"
  [[ "$name" == *.zip ]] && unzip -oq "$dir/$name" -d "$dir" && rm "$dir/$name"
  touch "$dir/.done"
}

# --- SAP (MIT): procurement-relevant tables only; skips sales, FI and the 1 GB dd03l dictionary
kaggle_files sap mustafakeser4/sap-dataset-bigquery-dataset \
  lfa1.csv adrc.csv adr6.csv but000.csv but020.csv \
  ekko.csv ekpo.csv eket.csv ekes.csv ekbe.csv ekkn.csv rbkp.csv rseg.csv rbco.csv \
  mara.csv makt.csv t023.csv t023t.csv t001.csv t001w.csv t005.csv t005t.csv \
  t006.csv tcurr.csv tcurc.csv tcurx.csv

# --- Entity-resolution benchmark (MIT per mirror; internal benchmarking only, never republish)
kaggle_files er_benchmark satwiksps/amazon-ml-challenge-2026 \
  dataset/train/train_source1.tsv dataset/train/train_source2.tsv \
  dataset/train/train_source3.tsv dataset/train/train_ground_truth.tsv

# --- Regional suppliers
kaggle_files pl_tenders michalpozoga/polish-public-tenders \
  contractors.parquet tenders_2024.parquet tenders_2025.parquet       # CC BY 4.0
kaggle_all   prozorro   oleksastepaniuk/prozorro-public-procurement-dataset   # CC0, ~2.8 GB

# --- Spend, documents, signals
kaggle_all   worldbank_awards theworldbank/corporate-procurement-contract-awards   # CC0 (2021 snapshot)
kaggle_all   company_docs     ayoubcherguelaine/company-documents-dataset         # Apache-2.0
kaggle_all   invoice_fraud    tokelomashile2/procurement-invoice-fraud-dataset    # Apache-2.0
kaggle_all   supply_chain     ayodejiibrahimlateef/supply-chain-datasets          # CC BY 4.0
kaggle_all   olist            olistbr/brazilian-ecommerce                          # CC BY-NC-SA 4.0
url          cuad https://zenodo.org/records/4595826/files/CUAD_v1.zip CUAD_v1.zip  # CC BY 4.0

# --- OFAC SDN list + aliases (US public domain)
if [[ ! -f "$RAW/ofac/.done" ]]; then
  mkdir -p "$RAW/ofac"; log "ofac: sdn.csv, alt.csv"
  curl -fsSL -o "$RAW/ofac/sdn.csv" https://www.treasury.gov/ofac/downloads/sdn.csv
  curl -fsSL -o "$RAW/ofac/alt.csv" https://www.treasury.gov/ofac/downloads/alt.csv
  touch "$RAW/ofac/.done"
else log "skip ofac (done)"; fi

# --- Manifest
log "writing manifest"
( cd "$RAW" && find . -type f ! -name '.done' ! -name 'MANIFEST.tsv' -printf '%P\t%s\n' | sort > MANIFEST.tsv )
log "done: $(du -sh "$RAW" | cut -f1) in $RAW"

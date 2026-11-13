#!/usr/bin/env bash
# Publish docs/case-study/index.html to GitHub Pages as <owner>/databricks-wizard-casestudy (same pattern as the
# WatchDog case study). Idempotent: creates the repo and Pages site on first run, updates index.html after that.
#   docs/case-study/deploy.sh [owner]
set -euo pipefail
OWNER=${1:-$(gh api user --jq .login)}
REPO="$OWNER/databricks-wizard-casestudy"
SRC="$(cd "$(dirname "$0")" && pwd)/index.html"

if ! gh repo view "$REPO" >/dev/null 2>&1; then
  gh repo create "$REPO" --public --description "Used to host the Databricks Wizard case study."
fi
# create or update index.html through the contents API (no local clone needed)
sha=$(gh api "repos/$REPO/contents/index.html" --jq .sha 2>/dev/null || true)
gh api -X PUT "repos/$REPO/contents/index.html" -f message="Update case study" \
  -f content="$(base64 -w0 "$SRC")" ${sha:+-f sha="$sha"} --jq .commit.sha
if ! gh api "repos/$REPO/pages" >/dev/null 2>&1; then
  gh api -X POST "repos/$REPO/pages" -f "source[branch]=main" -f "source[path]=/" --jq .html_url
fi
echo "https://${OWNER,,}.github.io/databricks-wizard-casestudy/ (Pages takes a minute to build)"

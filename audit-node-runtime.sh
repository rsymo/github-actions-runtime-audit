#!/usr/bin/env bash
#
# audit-node-runtime.sh
# Scan GitHub Actions workflow files in an org and flag action refs that map to
# older Node runtimes (including Node 20 deprecation targets).
#
# Usage:
#   ./audit-node-runtime.sh [org]
#   ./audit-node-runtime.sh rsymo-labs
#
# Output:
#   node-runtime-report.csv
#   node-runtime-summary.txt
#
# Requirements:
#   - gh CLI authenticated (gh auth status)
#

set -euo pipefail

ORG="${1:-rsymo-labs}"
REPORT="${REPORT:-node-runtime-report.csv}"
SUMMARY="${SUMMARY:-node-runtime-summary.txt}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command not found: $1" >&2
    exit 1
  fi
}

csv_escape() {
  # Escape RFC4180-like CSV fields.
  printf '%s' "$1" | sed 's/"/""/g'
}

decode_base64() {
  # macOS uses -D, Linux uses -d.
  if base64 --help 2>/dev/null | grep -q -- ' -d'; then
    base64 -d
  else
    base64 -D
  fi
}

require_cmd gh
require_cmd base64
require_cmd awk
require_cmd grep
require_cmd sed
require_cmd sort
require_cmd uniq

if ! gh auth status >/dev/null 2>&1; then
  echo "Error: gh is not authenticated. Run: gh auth login" >&2
  exit 1
fi

# Major versions known to be old for current Actions runtime policy.
# Keep this list current with:
# https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/
PATTERNS='actions/checkout@v([1-4])([^0-9]|$)|actions/setup-python@v([1-5])([^0-9]|$)|actions/setup-node@v([1-5])([^0-9]|$)|actions/upload-artifact@v([1-3])([^0-9]|$)|actions/download-artifact@v([1-4])([^0-9]|$)|actions/cache@v([1-3])([^0-9]|$)|actions/github-script@v([1-7])([^0-9]|$)|actions/labeler@v([1-4])([^0-9]|$)|actions/stale@v([1-8])([^0-9]|$)|actions/dependency-review-action@v([1-4])([^0-9]|$)|actions/setup-java@v([1-4])([^0-9]|$)|actions/setup-go@v([1-5])([^0-9]|$)|actions/setup-dotnet@v([1-3])([^0-9]|$)'

tmp_summary="$(mktemp)"
trap 'rm -f "$tmp_summary"' EXIT

printf 'repo,workflow,line,action\n' >"$REPORT"

echo "→ Listing repositories in $ORG..."
repos="$(gh repo list "$ORG" --limit 1000 --json nameWithOwner --jq '.[].nameWithOwner')"
repo_count="$(printf '%s\n' "$repos" | grep -c . || true)"
echo "  found $repo_count repos"

printf '%s\n' "$repos" | while IFS= read -r repo; do
  [ -z "$repo" ] && continue

  workflows="$(gh api "repos/$repo/contents/.github/workflows" --jq '.[] | select(.type=="file") | .path' 2>/dev/null || true)"
  [ -z "$workflows" ] && continue

  echo "  ▸ $repo"
  printf '%s\n' "$workflows" | while IFS= read -r wf; do
    [ -z "$wf" ] && continue

    content_b64="$(gh api "repos/$repo/contents/$wf" --jq '.content' 2>/dev/null || true)"
    [ -z "$content_b64" ] && continue
    content="$(printf '%s' "$content_b64" | tr -d '\n' | decode_base64 2>/dev/null || true)"
    [ -z "$content" ] && continue

    printf '%s\n' "$content" | awk -v regex="$PATTERNS" '
      $0 ~ /uses:[[:space:]]*/ && $0 ~ regex { print NR ":" $0 }
    ' | while IFS=: read -r line_no line_txt; do
      action="$(printf '%s\n' "$line_txt" | sed -E 's/.*uses:[[:space:]]*//; s/[[:space:]]+#.*$//; s/[[:space:]]+$//')"
      printf '"%s","%s","%s","%s"\n' \
        "$(csv_escape "$repo")" \
        "$(csv_escape "$wf")" \
        "$(csv_escape "$line_no")" \
        "$(csv_escape "$action")" >>"$REPORT"
      printf '%s\n' "$action" >>"$tmp_summary"
    done
  done
done

if [ -s "$tmp_summary" ]; then
  sort "$tmp_summary" | uniq -c | sort -rn >"$SUMMARY"
else
  : >"$SUMMARY"
fi

echo
echo "Done."
echo "  Detailed report: $REPORT"
echo "  Summary counts:  $SUMMARY"
if [ -s "$SUMMARY" ]; then
  echo
  echo "Top matches:"
  head -10 "$SUMMARY"
else
  echo "  No matches found for configured patterns."
fi

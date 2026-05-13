#!/usr/bin/env bash
set -euo pipefail

REPO_NAME="${1:-log-proxy}"
PRIVATE="${PRIVATE:-true}"
DESCRIPTION="${DESCRIPTION:-Lightweight OpenAI-compatible request/response log proxy for new-api -> CLIProxyAPI chains.}"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "GITHUB_TOKEN is required. Create a token with repo scope and export it first." >&2
  exit 1
fi

api() {
  curl -fsS \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@"
}

AUTH_LOGIN="$(api https://api.github.com/user | python -c 'import json,sys; print(json.load(sys.stdin)["login"])')"
OWNER="${GITHUB_OWNER:-$AUTH_LOGIN}"

payload="$(python - <<PY
import json
print(json.dumps({
  "name": "$REPO_NAME",
  "description": "$DESCRIPTION",
  "private": "$PRIVATE".lower() == "true",
  "auto_init": False,
}))
PY
)"

if [[ "$OWNER" == "$AUTH_LOGIN" ]]; then
  api -X POST https://api.github.com/user/repos -d "$payload" >/dev/null
else
  api -X POST "https://api.github.com/orgs/${OWNER}/repos" -d "$payload" >/dev/null
fi

if [[ ! -d .git ]]; then
  git init
fi

git checkout -B main
git add .
if git diff --cached --quiet; then
  echo "No changes to commit."
else
  git commit -m "Initial log proxy implementation"
fi

git remote remove origin 2>/dev/null || true
git remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${OWNER}/${REPO_NAME}.git"
git push -u origin main

echo "Published: https://github.com/${OWNER}/${REPO_NAME}"

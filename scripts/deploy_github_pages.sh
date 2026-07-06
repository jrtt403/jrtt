#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: scripts/deploy_github_pages.sh <github-username> <repo-name> [remote-url]" >&2
  echo "Example: scripts/deploy_github_pages.sh jrtt403 jrtt git@github.com:jrtt403/jrtt.git" >&2
  exit 1
fi

USERNAME="$1"
REPO="$2"
REMOTE_URL="${3:-git@github.com:${USERNAME}/${REPO}.git}"
BASE_URL="https://${USERNAME}.github.io/${REPO}"

python3 src/jrtt/cli.py publish --article latest --base-url "${BASE_URL}"

if [ ! -d .git ]; then
  git init
  git branch -M main
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "${REMOTE_URL}"
else
  git remote set-url origin "${REMOTE_URL}"
fi

git add .github .gitignore .env.example README.md WORK_CONTEXT.md articles config docs drafts prompts public scripts src
git status --short

echo
echo "Next commands:"
echo "  git commit -m \"Deploy JR Toutiao content site\""
echo "  git push -u origin main"
echo
echo "After the GitHub Actions run finishes, your feed should be:"
echo "  ${BASE_URL}/feed.xml"

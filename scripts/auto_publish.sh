#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 src/jrtt/cli.py auto \
  --count "${JRTT_AUTO_COUNT:-1}" \
  --min-article-chars "${JRTT_MIN_ARTICLE_CHARS:-1000}" \
  --deploy \
  --commit-message "${JRTT_COMMIT_MESSAGE:-Auto publish generated article}"

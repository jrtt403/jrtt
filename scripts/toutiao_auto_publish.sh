#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python_bin="${JRTT_PYTHON:-python3}"
if [[ -x ".venv/bin/python" && -z "${JRTT_PYTHON:-}" ]]; then
  python_bin=".venv/bin/python"
fi

international_count="${JRTT_INTERNATIONAL_COUNT:-5}"
china_count="${JRTT_CHINA_COUNT:-5}"
candidate_limit="${JRTT_CANDIDATE_LIMIT:-50}"
min_chars="${JRTT_MIN_ARTICLE_CHARS:-1000}"
international_min_score="${JRTT_INTERNATIONAL_MIN_SCORE:-23}"
china_min_score="${JRTT_CHINA_MIN_SCORE:-21}"
allow_unenriched_arg=""
if [[ -n "${JRTT_ALLOW_UNENRICHED:-}" ]]; then
  allow_unenriched_arg="--allow-unenriched"
fi

generated_output="$({
"$python_bin" src/jrtt/cli.py auto \
  --count "$international_count" \
  --category international \
  --candidate-limit "$candidate_limit" \
  --min-score "$international_min_score" \
  --min-article-chars "$min_chars" \
  ${allow_unenriched_arg:+$allow_unenriched_arg}

"$python_bin" src/jrtt/cli.py auto \
  --count "$china_count" \
  --category china \
  --candidate-limit "$candidate_limit" \
  --min-score "$china_min_score" \
  --min-article-chars "$min_chars" \
  --no-fetch \
  ${allow_unenriched_arg:+$allow_unenriched_arg}
})"
printf '%s\n' "$generated_output"

articles=()
while IFS= read -r line; do
  if [[ "$line" == *.md && -f "$line" ]]; then
    articles+=("$line")
  fi
done <<< "$generated_output"

if [[ "${#articles[@]}" -eq 0 ]]; then
  echo "No generated article files found in auto output." >&2
  exit 1
fi

"$python_bin" src/jrtt/cli.py deploy \
  --article all \
  --message "${JRTT_COMMIT_MESSAGE:-Auto publish generated articles}"

headless_arg=(--headless)
if [[ "${JRTT_TOUTIAO_HEADLESS:-1}" == "0" ]]; then
  headless_arg=()
fi

interval="${JRTT_TOUTIAO_INTERVAL_SECONDS:-120}"
for index in "${!articles[@]}"; do
  article="${articles[$index]}"
  echo "Publishing to Toutiao [$((index + 1))/${#articles[@]}]: $article"
  "$python_bin" scripts/toutiao_publish_playwright.py \
    --article "$article" \
    --confirm-publish \
    "${headless_arg[@]}"

  if [[ "$index" -lt "$((${#articles[@]} - 1))" ]]; then
    echo "Waiting ${interval}s before next Toutiao submission..."
    sleep "$interval"
  fi
done

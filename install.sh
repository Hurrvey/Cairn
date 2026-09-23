#!/usr/bin/env bash
set -euo pipefail
command -v uv >/dev/null 2>&1 || { echo "Install uv before running setup." >&2; exit 1; }
if [ -f .env ]; then
    echo "Keeping existing .env unchanged."
else
    uv run python scripts/init_env.py
fi
uv run python scripts/prepare_embedding_model.py
printf '\nPrepared local model and configuration.\n'
printf 'Start: docker compose --profile local-model up -d --build\n'
printf 'Read bootstrap logs: docker compose logs migrate\n'
printf 'Open http://127.0.0.1:8080 and change the initial password.\n'
printf 'Configure a TEI provider at http://embedding:80, model dimension 384, tokenizer minilm.\n'
printf 'Keep .env private. Do not expose this local HTTP preset without HTTPS.\n'

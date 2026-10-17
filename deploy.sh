#!/usr/bin/env bash
# Usage: ./deploy.sh <gcp-project-id> [region]   (needs: gcloud auth login)
set -euo pipefail
: "${ADMIN_TOKEN:?Set ADMIN_TOKEN before deploying a public service}"
PROJECT="${1:?project id}"; REGION="${2:-asia-south1}"
set -a; source my_agent/.env; set +a
gcloud run deploy godown-agent --source . --project "$PROJECT" --region "$REGION" \
  --allow-unauthenticated --memory 1Gi --cpu 2 \
  --set-env-vars "ADMIN_TOKEN=${ADMIN_TOKEN:-},GOOGLE_API_KEY=${GOOGLE_API_KEY},JEV_API_KEY=${JEV_API_KEY:-},GOOGLE_MAPS_API_KEY=${GOOGLE_MAPS_API_KEY:-},USE_REAL_MAPS=${USE_REAL_MAPS:-0}"

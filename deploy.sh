#!/bin/bash
# One-command deploy for open-reviewer
# Prerequisites: brew install flyctl && flyctl auth signup

set -e

echo "🚀 Deploying Open Reviewer Agent..."

if ! command -v flyctl &> /dev/null; then
    echo "❌ flyctl not installed."
    echo "   brew install flyctl"
    echo "   flyctl auth signup"
    exit 1
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "❌ ANTHROPIC_API_KEY not set"
    exit 1
fi

echo "🔐 Setting secrets..."
flyctl secrets set ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
flyctl secrets set WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 32)}"

echo "📦 Deploying..."
flyctl deploy

echo ""
echo "✅ Deployed! Webhook URL: https://open-reviewer.fly.dev/webhook"

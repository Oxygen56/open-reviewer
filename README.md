# Open Reviewer

AI code review agent service. Receives GitHub webhook events (PR opened / synchronize), runs a Claude Code agent with the **oss-pr-reviewer** skill against the diff, and posts structured review results back to the PR as comments.

## Architecture

```
GitHub Webhook → Rate Limiter → Store (SQLite) → Context Engine → Pipeline → GitHub Comments
                    │              │                   │              │
                    ▼              ▼                   ▼              ▼
              Per-PR cooldown  Idempotency      prioritize_diff()  Reviewer→Verifier
              Concurrency cap  SHA-based dedup  Security-first     (adversarial)
```

| Tier | Modules | Interview Talking Point |
|------|---------|------------------------|
| **Agent Intelligence** | `pipeline.py`, `context_engine.py`, `agent.py` | Multi-agent adversarial verification, 4-tier context budget |
| **Production Ops** | `store.py`, `ratelimit.py`, `cost.py`, `auth.py` | SQLite persistence (zero infra), per-repo cooldown, token cost tracking, API auth |
| **Observability** | `observability.py` | OpenTelemetry tracing, P50/P99 latency, structured JSON logs |
| **Quality Assurance** | `evaluation/` | Gold set testing with real OSS bugs, recall/precision/F1 |
| **Design Docs** | `adr/` | 5 Architecture Decision Records with trade-off analysis |
| **Deployment** | `Dockerfile`, `fly.toml`, `deploy.sh` | One-command deploy to fly.io |

## Quick Start

### One-line deploy (fly.io)

```bash
fly launch --from https://github.com/Oxygen56/open-reviewer
```

### Local development

```bash
# 1. Clone the repo
git clone https://github.com/Oxygen56/open-reviewer.git
cd open-reviewer

# 2. Also clone the oss-pr-reviewer skill
git clone https://github.com/Oxygen56/oss-pr-reviewer.git

# 3. Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Set environment variables
export ANTHROPIC_API_KEY="sk-ant-..."
export GITHUB_TOKEN="ghp_..."
export WEBHOOK_SECRET="..."
export SKILL_PATH="./oss-pr-reviewer"

# 5. Run the server
uvicorn server:app --reload --port 8000
```

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key for Claude Code |
| `GITHUB_TOKEN` | Yes | GitHub personal access token (repo / pull_requests write scope) |
| `WEBHOOK_SECRET` | No | GitHub webhook secret (HMAC-SHA256) -- strongly recommended |
| `SKILL_PATH` | No | Path to oss-pr-reviewer skill (default: `/app/oss-pr-reviewer`) |
| `HOST` | No | Bind address (default: `0.0.0.0`) |
| `PORT` | No | Listen port (default: `8000`) |

## Docker

### Build

```bash
docker build -t open-reviewer .
```

### Run

```bash
docker run -d --name open-reviewer \
  -p 8000:8000 \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e GITHUB_TOKEN="ghp_..." \
  -e WEBHOOK_SECRET="..." \
  -v /path/to/oss-pr-reviewer:/app/oss-pr-reviewer \
  open-reviewer
```

### Docker Compose

```yaml
version: "3.9"
services:
  reviewer:
    image: open-reviewer
    ports:
      - "8000:8000"
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      GITHUB_TOKEN: ${GITHUB_TOKEN}
      WEBHOOK_SECRET: ${WEBHOOK_SECRET}
    volumes:
      - ./oss-pr-reviewer:/app/oss-pr-reviewer
```

## Setting up GitHub Webhooks

1. Go to your repository **Settings > Webhooks > Add webhook**.
2. **Payload URL**: `https://your-deployment.com/webhook`
3. **Content type**: `application/json`
4. **Secret**: The same value you set for `WEBHOOK_SECRET`
5. **Events**: Select **"Let me select individual events"** and check **"Pull requests"**.
6. **Active**: Yes.

The service automatically listens for `opened`, `synchronize`, `ready_for_review`, and `reopened` events.

## Manual Trigger

Trigger a review for any public or accessible PR:

```bash
curl -X POST https://your-deployment.com/review/owner/repo/42
```

Check review status:

```bash
curl https://your-deployment.com/review/owner/repo/42
```

## API Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `POST` | `/webhook` | GitHub webhook receiver |
| `POST` | `/review/{owner}/{repo}/{pr}` | Manually trigger a review |
| `GET` | `/review/{owner}/{repo}/{pr}` | Review status |
| `GET` | `/reviews` | List all tracked reviews |

## Output

The review produces structured JSON:

```json
{
  "summary": "...",
  "findings": [
    {
      "severity": "critical|warning|info",
      "message": "...",
      "path": "file.py",
      "line": 42,
      "commit_id": null
    }
  ],
  "mergeability_score": 0.85
}
```

This is posted to the PR as:
- A summary comment with mergeability score and finding counts
- Individual inline review comments where line-level information is available

## Development

### Running tests

```bash
pytest -v
```

### Project structure

```
open-reviewer/
  server.py          # FastAPI webhook receiver
  agent.py           # Claude Agent SDK integration
  github_client.py   # GitHub API client
  requirements.txt   # Python dependencies
  Dockerfile         # Container image
  tests/
    test_review.py   # Unit + integration tests
```

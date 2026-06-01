# ADR 002: Fire-and-Forget Webhook Processing

## Status
Accepted (2026-06-01)

## Context
GitHub webhooks have a 10-second timeout. If we don't respond within 10s, GitHub retries the delivery, potentially triggering duplicate reviews. An LLM-based review typically takes 30-120 seconds.

## Options Considered

1. **Fire-and-forget**: Return HTTP 202 immediately, process in background.
2. **Synchronous with timeout**: Process within 10s, fail if timeout.
3. **Task queue**: Enqueue to Redis/RabbitMQ, worker picks up.

## Decision
**Fire-and-forget with asyncio background task**, documented as "good for single-instance, upgrade to task queue for production."

## Rationale

The progression is intentional:

```
MVP (current):    asyncio.ensure_future → 202 Accepted
Production:       Celery/Redis Queue → worker pool
Enterprise:       Pub/Sub → fan-out to region-specific workers
```

For MVP:
- Zero infrastructure dependencies (no Redis, no message broker)
- Simple to deploy (single container)
- Acceptable failure mode (lost review on crash, next webhook retriggers)

The `_jobs` dict provides idempotency: duplicate webhook deliveries for the same PR are detected and skipped.

## Consequences
- Review results not durable across process restarts
- No backpressure (burst of webhooks could overload agent)
- Explicitly documented as MVP limitation in code comments
- Migration path to task queue is straightforward (replace `asyncio.ensure_future` with `enqueue`)

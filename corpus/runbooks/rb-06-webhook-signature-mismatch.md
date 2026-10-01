---
id: RB-06
title: Webhook Signature Mismatch After Secret Rotation
severity: SEV1
services: [webhook-handler, payment-gateway, laravel-backend, nginx, postgresql]
lang: en
tags: [payment, webhook, hmac, secret-rotation, acquirer, sbp]
---

# Webhook Signature Mismatch After Secret Rotation

## Симптом

Incoming acquirer webhooks are rejected with HTTP 401. Payment confirmations stop flowing: users report cards not being topped up despite successful SBP transfers. Alert fires: `acquirer_webhook_failures_total > 50` over 5 minutes.

Example log line from the webhook handler container:

```
[2026-06-23 03:17:42] production.ERROR: HMAC signature mismatch {"txn_id":"tx_8f3ac1","user_id":10042,"received_sig":"a3f9...","expected_sig":"d71c..."} {"file":"WebhookController.php","line":84}
```

## Область и влияние

All incoming payment confirmations from the acquirer are blocked. Any SBP top-up initiated after the secret rotation is stuck in `pending` state. Funds have left the user's bank account but balances on virtual cards (PAN masked in logs, e.g. `card ****1111`) are not credited. Financial impact is direct and ongoing. SEV1.

## Диагностика

**1. Confirm 401 rate spike:**
```bash
docker logs --since=30m webhook-handler 2>&1 | grep -c "signature mismatch"
```

**2. Check which service holds the stale secret — compare env vars across containers:**
```bash
for c in webhook-handler payment-gateway; do
  echo "=== $c ==="; docker exec $c env | grep ACQUIRER_WEBHOOK_SECRET
done
```

**3. LogQL in Loki (Grafana Explore):**
```
{job="webhook-handler"} |= "signature mismatch" | json | line_format "{{.txn_id}} {{.user_id}}"
```

**4. Grafana panel:** `Payments > Webhook / Acquirer` — watch `acquirer_webhook_401_rate` and `payment_confirmations_per_minute` dropping to zero.

**5. Find stuck transactions in PostgreSQL:**
```sql
SELECT txn_id, user_id, amount_usd, status, created_at
FROM transactions
WHERE status = 'pending'
  AND created_at > NOW() - INTERVAL '2 hours'
ORDER BY created_at DESC
LIMIT 20;
-- Expected: txn_id=tx_8f3ac1, user_id=10042, amount_usd=12.50
```

## Решение

**Immediate fix:**

1. Identify the container still using the old secret (step 2 above).
2. Update the secret in the affected service's environment:
```bash
# Edit docker-compose.override.yml or .env, then:
docker compose up -d --no-deps webhook-handler
```
3. Verify the new secret is active:
```bash
docker exec webhook-handler env | grep ACQUIRER_WEBHOOK_SECRET
```
4. Ask the acquirer to replay the failed webhooks for the affected window, or trigger manual reprocessing:
```bash
docker exec laravel-backend php artisan webhooks:replay --since="2 hours ago" --status=failed
```
   Replay MUST be idempotent: ensure handler dedups by `txn_id` so already-credited transactions are not topped up twice. Re-confirm only `pending` transactions; skip any already in a terminal state.
5. Monitor `acquirer_webhook_401_rate` — should drop to zero within 2 minutes.

**Temporary workaround (if replay is not available):** Manually reconcile stuck `pending` transactions via the acquirer's back-office portal and update statuses directly in PostgreSQL under DBA supervision. Mark as `[ВРЕМЕННЫЙ]`.

**Prevention:** Add a smoke-test step to the secret-rotation runbook: send a test webhook with the new HMAC and assert HTTP 200 before retiring the old secret.

## Эскалация

- **L1:** Confirm symptom via Grafana dashboard and user reports. Check `acquirer_webhook_401_rate`. Do not attempt fixes. Escalate to L2 immediately with: alert screenshot, number of affected `pending` txns from SQL above, and timeframe.
- **L2:** Run diagnostic steps 1–5. Identify stale-secret container. Apply fix (redeploy container with correct secret). Trigger webhook replay. Confirm metric recovery. If replay unavailable or >200 stuck transactions, escalate to L3.
- **L3 (on-call DevOps + backend lead):** Attach: full `docker logs webhook-handler` dump, Loki export, SQL result of stuck transactions, acquirer support ticket number. Authorize manual reconciliation if needed. Review secret-rotation procedure for systemic fix.

## Связано

- [RB-01 — Payment webhook lost/duplicated (idempotency)](rb-01-webhook-dedup.md)
- [RB-02 — SBP top-up stuck in PENDING](rb-02-sbp-pending-timeout.md)

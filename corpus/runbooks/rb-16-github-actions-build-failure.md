---
id: RB-16
title: "GitHub Actions pipeline fails: docker build or lint step red on push"
severity: SEV3
services: [ci-cd, docker, github-actions, gitlab-ci]
lang: en
tags: [ci, docker, build, lint, github-actions, gitlab-ci, devops]
---

# GitHub Actions Pipeline Fails: Docker Build or Lint Step Red on Push

## Симптом

A developer pushes a commit or opens a PR and the GitHub Actions pipeline turns red. The failing step is either the Docker image build job or a lint/static-analysis step (e.g., PHP CS Fixer, PHPStan, hadolint). The pipeline status badge on the repository shows "failing". No production service is affected yet, but the broken pipeline blocks all merges to `main`/`release`.

Example log line from a failed build step:

```
ERROR [build 5/9] RUN composer install --no-dev --optimize-autoloader
#12 1.843 Your requirements could not be resolved to an installable set of packages.
#12 1.843
#12 1.843   Problem 1
#12 1.843     - Root composer.json requires php ^8.1 but your php version (8.0.28) does not satisfy that requirement.
exit code: 1
```

## Область и влияние

Affects all engineers pushing to the affected repository. No end-user-facing services, payments, or card operations are impacted. Delivery pipeline is blocked — hotfixes cannot be promoted to production until fixed. Severity escalates to SEV2 if a critical fix is stuck in queue for more than 2 hours.

## Диагностика

**1. Read the full Actions log in the GitHub UI.**

Open `Actions → <failed workflow run> → <failed job>` and expand the red step. Note the exact error message and line number.

**2. Identify the failing step category.**

- Docker build failure → look for `COPY`, `RUN`, or `FROM` errors in the log.
- Lint failure → look for the linter binary name (`phpcs`, `phpstan`, `hadolint`).

**3. Reproduce locally.**

```bash
# Clone the branch and reproduce the build
git checkout <failing-branch>

# Docker build (mirrors CI context)
DOCKER_BUILDKIT=1 docker build --no-cache -t platipay/app:local .

# Lint: PHP CS Fixer
docker run --rm -v "$(pwd)":/app php:8.1-cli \
  ./vendor/bin/php-cs-fixer fix --dry-run --diff

# Lint: hadolint (Dockerfile linter)
docker run --rm -i hadolint/hadolint < Dockerfile
```

**4. Check runner environment divergence.**

```bash
# Compare local vs CI base image
grep -E "^FROM" Dockerfile

# Check composer.lock PHP platform requirement
cat composer.lock | python3 -m json.tool | grep '"php"' | head -5
```

**5. Grafana / metrics** (CI-host node health, if self-hosted runner):

Open `Grafana → Node Exporter → CPU/Mem` panel for the runner host. Metric to watch: `node_memory_MemAvailable_bytes` — OOM during build causes silent exit code 137.

```
avg(rate(node_cpu_seconds_total{mode!="idle", instance="runner-host:9100"}[5m])) > 0.95
```

**6. Check recent dependency or base-image changes.**

```bash
git log --oneline -10 -- Dockerfile composer.lock package-lock.json
```

## Решение

**Case A: PHP version mismatch in Dockerfile.**

Update `FROM` to match the platform requirement:

```dockerfile
- FROM php:8.0-fpm-alpine
+ FROM php:8.1-fpm-alpine
```

Commit, push, verify pipeline goes green.

**Case B: Lint rule violation.**

Run the linter locally with auto-fix where safe:

```bash
./vendor/bin/php-cs-fixer fix
git diff  # review, then commit
```

**Case C: Transient runner failure (OOM / disk full).**

Temporary workaround: re-run the failed job via `Actions → Re-run failed jobs`. If it passes on retry, open a ticket to increase runner disk/memory quota.

**Case D: Broken third-party base image layer.**

Pin to a specific digest:

```dockerfile
FROM php:8.1-fpm-alpine@sha256:<digest>
```

> **Note:** All steps above transfer directly to GitLab CI runners. Replace `Actions → Re-run` with `CI/CD → Pipelines → Retry`. Runner logs: `gitlab-runner` systemd unit or `docker logs gitlab-runner`.

## Эскалация

- **L1:** Confirm the pipeline is red and identify the failing step name. Check if the same branch was green in a prior run (transient vs. permanent failure). If transient — retry and monitor. If permanent — escalate to L2 with a link to the failing run and a copy of the error log.
- **L2:** Reproduce the build locally following Диагностика steps. Identify root cause (version drift, lint violation, base-image change). Apply fix from Решение. If the fix requires a Dockerfile or CI config change affecting other teams, coordinate before merging. Attach: local reproduction output, proposed fix diff.
- **L3 (Duty DevOps):** Engage when: the fix is not obvious after 1 hour of L2 investigation; a base infrastructure image (internal registry) is broken affecting multiple repositories; runner host is unhealthy (OOM/disk/GPU — check `node_memory_MemAvailable_bytes` and `node_filesystem_avail_bytes` in Grafana). Attach: runner host metrics screenshot, `docker system df` output, last 50 lines of the runner log.

## Связано

- [RB-09 — vLLM OOM — container crash (VRAM)](rb-09-vllm-oom-crash.md)
- [RB-11 — FastAPI facade 502 — vLLM upstream down](rb-11-fastapi-502-vllm-upstream.md)

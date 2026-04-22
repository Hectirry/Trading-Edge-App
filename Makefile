.PHONY: help deploy-staging rollback-staging check-staging logs-engine logs-telegram ps

help:
	@echo "targets:"
	@echo "  deploy-staging    — build and restart tea-engine + tea-telegram-bot"
	@echo "  rollback-staging  — revert last commit and redeploy"
	@echo "  check-staging     — health check (engine up, heartbeat fresh)"
	@echo "  logs-engine       — tail tea-engine logs"
	@echo "  logs-telegram     — tail tea-telegram-bot logs"
	@echo "  ps                — docker compose ps"

deploy-staging:
	git pull --rebase
	docker compose build tea-engine tea-telegram-bot
	docker compose up -d tea-postgres tea-redis
	docker compose up -d tea-ingestor
	docker compose up -d --force-recreate tea-engine tea-telegram-bot
	@sleep 15
	@./scripts/check_staging_health.sh || ( \
		echo "health check failed, rolling back"; \
		$(MAKE) rollback-staging; \
		exit 1 \
	)
	@echo "deploy ok"

rollback-staging:
	git reset --hard HEAD~1
	docker compose build tea-engine tea-telegram-bot
	docker compose up -d --force-recreate tea-engine tea-telegram-bot

check-staging:
	@./scripts/check_staging_health.sh

logs-engine:
	docker compose logs -f --tail=200 tea-engine

logs-telegram:
	docker compose logs -f --tail=200 tea-telegram-bot

ps:
	docker compose ps

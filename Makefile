.PHONY: seed migrate makemigrations runserver ingest refresh test shell tailwind \
        docker-build docker-up docker-down docker-logs docker-shell docker-test \
        docker-migrate docker-ingest docker-prod

PY := python

seed:
	$(PY) manage.py loaddata apps/catalog/fixtures/seed.json

migrate:
	$(PY) manage.py migrate

makemigrations:
	$(PY) manage.py makemigrations

runserver:
	$(PY) manage.py runserver

ingest:
	$(PY) manage.py shell -c "from apps.sources.tasks import ingest_mcp_registry; ingest_mcp_registry()"

refresh:
	$(PY) manage.py shell -c "from apps.search.tasks import refresh_search_vectors_batch; refresh_search_vectors_batch()"

test:
	pytest -q

shell:
	$(PY) manage.py shell

# Docker commands
docker-build:
	docker-compose build

docker-up:
	docker-compose up -d
	@echo "🚀 Services started!"
	@echo "📱 Web: http://localhost:8000"
	@echo "👤 Admin: http://localhost:8000/admin (admin:admin123)"

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f web worker beat

docker-shell:
	docker-compose exec web $(PY) manage.py shell

docker-test:
	docker-compose exec web pytest -q

docker-migrate:
	docker-compose exec web $(PY) manage.py migrate

docker-ingest:
	docker-compose exec web make ingest

docker-prod:
	docker-compose --profile production up -d

# Full development setup
setup: docker-build docker-up
	@echo "⏳ Waiting for services to be ready..."
	@sleep 10
	@echo "✅ Setup complete!"
	@echo "📱 Web: http://localhost:8000"
	@echo "👤 Admin: http://localhost:8000/admin"

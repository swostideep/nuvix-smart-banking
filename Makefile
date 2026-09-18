# Common tasks. `make help` lists them.

PYTHON := .venv/bin/python
MANAGE := $(PYTHON) manage.py
DEV    := DJANGO_SETTINGS_MODULE=config.settings.dev

.DEFAULT_GOAL := help
.PHONY: help venv install migrate migrations run seed test cov lint fmt check \
        schema shell worker beat up down logs clean train

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv (uses uv if available, else stdlib venv)
	@command -v uv >/dev/null 2>&1 \
		&& uv venv --python 3.12 .venv \
		|| python3 -m venv .venv

install: ## Install development dependencies
	@command -v uv >/dev/null 2>&1 \
		&& VIRTUAL_ENV=$(PWD)/.venv uv pip install -r requirements-dev.txt \
		|| $(PYTHON) -m pip install -r requirements-dev.txt

migrations: ## Generate migrations
	$(DEV) $(MANAGE) makemigrations

migrate: ## Apply migrations
	$(DEV) $(MANAGE) migrate

run: ## Run the development server
	$(DEV) $(MANAGE) runserver 0.0.0.0:8000

seed: ## Seed a coherent demo dataset
	$(DEV) $(MANAGE) seed_demo --users 120 --flush

superuser: ## Create an admin user
	$(DEV) $(MANAGE) createsuperuser

test: ## Run the test suite
	$(PYTHON) -m pytest

cov: ## Run tests with a coverage report
	$(PYTHON) -m pytest --cov=nuvix --cov-report=term-missing --cov-report=html

lint: ## Lint
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

fmt: ## Format and auto-fix
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check . --fix

check: lint test ## Lint then test -- what CI runs

schema: ## Write the OpenAPI schema to openapi.yaml
	$(DEV) $(MANAGE) spectacular --file openapi.yaml

shell: ## Django shell
	$(DEV) $(MANAGE) shell

worker: ## Run a Celery worker
	$(DEV) $(PYTHON) -m celery -A config worker --loglevel=info

beat: ## Run the Celery beat scheduler
	$(DEV) $(PYTHON) -m celery -A config beat --loglevel=info

train: ## Train the credit-intelligence models (needs requirements-ml.txt)
	$(DEV) $(PYTHON) scripts/train_models.py --data data/Loan_default.csv

up: ## Start the full stack in Docker
	docker compose up --build -d
	docker compose exec api python manage.py seed_demo --users 120

down: ## Stop the stack
	docker compose down

logs: ## Tail the stack logs
	docker compose logs -f

clean: ## Remove build and test artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov staticfiles

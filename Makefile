.PHONY: dev test build up down logs sandbox-image

dev:            ## run control plane locally (needs .env)
	uvicorn app.main:app --reload --port $${PORT:-8080}

test:           ## unit tests with fake LLM + dev sandbox driver
	SANDBOX_DRIVER=unsafe_local ALLOW_UNSAFE_LOCAL_SANDBOX=1 python -m pytest -q

sandbox-image:  ## build the sandbox image
	docker build -t brz-sandbox:latest sandbox

build: sandbox-image
	docker compose build

up: build       ## start on the Vultr VM
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f control-plane

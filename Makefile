.PHONY: install test test-neo4j lint run run-fake eval live-llm

install:
	pip install -e ".[dev]"

test:
	pytest -q

# needs a reachable Neo4j, e.g. `docker compose up neo4j` then NEO4J_TEST_PASSWORD=please-change-me
test-neo4j:
	NEO4J_TEST_URI=$${NEO4J_TEST_URI:-bolt://localhost:7687} NEO4J_TEST_USER=$${NEO4J_TEST_USER:-neo4j} pytest -q -m neo4j tests/test_repo_contract.py

lint:
	ruff check app tests eval scripts

run:
	uvicorn app.main:app --reload --port 8000

run-fake:
	LLM_BACKEND=fake BRAIN_BACKEND=memory uvicorn app.main:app --reload --port 8000

eval:
	python -m eval.run_eval --backend fake

live-llm:
	python -m scripts.live_llm_check

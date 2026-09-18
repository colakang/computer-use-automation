.PHONY: setup mock discover replay test evidence stability

setup:
	uv sync
	uv run playwright install chromium
	test -f .env || cp .env.example .env

mock:
	uv run cua mock

discover:
	uv run cua discover goals/savings_balance.yaml

replay:
	uv run cua replay member.savings_balance.read -t harbor -i member_id=10042

test:
	uv run pytest -q

evidence:
	uv run python scripts/make_evidence.py

stability:
	uv run python scripts/stability.py 20

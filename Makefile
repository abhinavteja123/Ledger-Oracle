.PHONY: gen run test eval faults ablate sweep audit demo

gen:
	python -m eval.generate --seed 1337 --out data/

run:
	uvicorn app:app --reload

test:
	python -m pytest -q

eval:
	python -m eval.score --data data/

faults:
	python -m eval.score --data data/ --inject-failure db_timeout
	python -m eval.score --data data/ --inject-failure db_unavailable
	python -m eval.score --data data/ --inject-failure malformed_row
	python -m eval.score --data data/ --inject-failure contradictory

ablate:
	python -m eval.ablation --data data/

sweep:
	python -m eval.sweep --data data/ --fuzz 0,1,2

audit:
	python -m eval.seed_audit --data data/ --path audit.jsonl
	python -m audit.verify audit.jsonl

demo: gen test eval faults audit
	-python -m eval.ablation --data data/

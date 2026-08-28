"""Tamper one event's payload in place, leaving its stored hash untouched -- so
audit.verify catches the mismatch. The demo beat for PRD 12.4.

Usage: python -m eval.tamper --event 417 --field amount_paise --to 999900
"""
import argparse
import json


def tamper(path: str, event_number: int, field: str, value) -> None:
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    idx = event_number - 1
    record = json.loads(lines[idx])
    record["payload"][field] = value  # mutate content, leave "hash" as originally computed
    lines[idx] = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", type=int, required=True)
    ap.add_argument("--field", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--path", default="audit.jsonl")
    args = ap.parse_args()
    try:
        value = int(args.to)
    except ValueError:
        value = args.to
    tamper(args.path, args.event, args.field, value)
    print(f"Tampered event {args.event}: payload.{args.field} -> {value}")


if __name__ == "__main__":
    main()

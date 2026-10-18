"""python -m app.ai.evals [--provider mock|anthropic|bedrock] [--model ID] [--report path.json]"""
import argparse
import asyncio
import dataclasses
import json
import sys

from app.ai import llm
from app.ai.evals import run_evals


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden-set evals for discharge-summary generation")
    parser.add_argument("--provider", default="mock", choices=["mock", "anthropic", "bedrock"])
    parser.add_argument("--model", help="model id (default: LLM_SUMMARY_MODEL, else claude-sonnet-5-5)")
    parser.add_argument("--report", help="write the full JSON report to this path")
    args = parser.parse_args()

    overrides = {"llm_provider": args.provider}
    if args.model:
        overrides["summary_model"] = args.model
    llm.settings = dataclasses.replace(llm.settings, **overrides)
    client = llm.get_llm_client("summary")

    report = asyncio.run(run_evals(client))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

    s = report["summary"]
    print(f"evals: {report['prompt_version']} on {report['provider']}/{report['model_id']}")
    for r in report["results"]:
        notes = []
        if r["missing_facts"]:
            notes.append(f"missing {r['missing_facts']}")
        if r["hallucinations"]:
            notes.append(f"hallucinated {[h['value'] for h in r['hallucinations']]}")
        if r["forbidden_found"]:
            notes.append(f"forbidden {r['forbidden_found']}")
        if r["phi_leaks"]:
            notes.append(f"PHI sent to model {r['phi_leaks']}")
        if r["status"] != "succeeded":
            notes.append(r["status"])
        print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['id']:<28} coverage={r['coverage']:.2f} {'; '.join(notes)}")
    print(f"cases={s['cases']} passed={s['passed']} mean_coverage={s['mean_coverage']} "
          f"hallucinations={s['hallucinations']} phi_leaks={s['phi_leaks']} failures={s['failures']} "
          f"tokens={s['input_tokens']}/{s['output_tokens']}")
    failed = [name for name, ok in report["checks"].items() if not ok]
    print("RESULT: " + ("ok" if report["ok"] else f"FAILED ({', '.join(failed)})"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

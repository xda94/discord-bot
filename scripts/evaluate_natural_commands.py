#!/usr/bin/env python3
"""Read-only corpus evaluation. No Discord calls or action execution."""
import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from natural_commands import parse_natural_command, NaturalClarification, NaturalCommandError
from action_proposals import authorize_proposal, validate_proposal


def evaluate(cases, generate=None):
    correct = unauthorized = unavailable = 0
    by_language = {}
    for index, case in enumerate(cases, 1):
        text = case['text']
        expected = case.get('llm_expected', case['expected']) if generate else case['expected']
        parsed = parse_natural_command(text)
        observed = 'conversation' if parsed is None else type(parsed).__name__
        if generate:
            result = generate(text)
            if result is None:
                unavailable += 1
                observed = 'unavailable'
            elif result.action:
                try:
                    proposal = validate_proposal(result.action)
                    authorize_proposal(result.action, text)
                    observed = type(proposal).__name__
                    if expected == 'conversation':
                        unauthorized += 1
                    if parsed is not None and is_dataclass(parsed) and not isinstance(parsed, (NaturalClarification, NaturalCommandError)):
                        if asdict(proposal) != asdict(parsed):
                            observed = 'incorrect_fields'
                except (ValueError, TypeError, KeyError):
                    observed = 'invalid'
            else:
                observed = 'conversation'
        good = observed == expected
        correct += good
        language = case.get('language', 'en')
        group = by_language.setdefault(language, {'cases': 0, 'correct': 0})
        group['cases'] += 1
        group['correct'] += good
        if generate and index % 10 == 0:
            print(f'Evaluated {index}/{len(cases)} cases', file=sys.stderr, flush=True)
    rate = correct / len(cases) if cases else 0
    return {'route': 'model' if generate else 'parser', 'cases': len(cases),
            'correct': correct, 'accuracy': rate, 'unauthorized_actions': unauthorized,
            'unavailable': unavailable, 'by_language': by_language,
            'passed': len(cases) >= 100 and rate >= .95 and unauthorized == 0 and unavailable == 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Evaluate the configured local model; remains read-only')
    parser.add_argument('--model', help='Allowed deployed model alias')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'data/natural-command-eval.json')
    args = parser.parse_args()
    cases = json.loads(args.corpus.read_text())
    generate = None
    if args.live:
        # Enable the action *proposal schema* only in this read-only process.
        os.environ['NATURAL_LLM_ENABLED'] = '1'
        from llm.responses import generate_mention_result
        generate = lambda text: generate_mention_result('Corpus evaluation', text, context_messages=[], model=args.model)
    report = evaluate(cases, generate)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

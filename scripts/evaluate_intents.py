#!/usr/bin/env python3
"""Offline semantic evaluation: scripted controls or exact request-bound replay.

For fresh inference import run_case and inject an explicitly authorized callback.
This CLI neither discovers credentials nor contacts model/provider services.
"""
import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from semantic_oracle import aggregate,fingerprint,expected_rows

CORPUS=ROOT/'benchmarks/intent-v2'
MANIFEST=CORPUS/'manifest.json'


def source_hashes():
    paths=[*sorted((ROOT/'src').rglob('*.py')),*sorted((ROOT/'src').rglob('*.json')),
           *sorted((ROOT/'tests').rglob('*.py')),*sorted((ROOT/'tests').rglob('*.json')),
           ROOT/'pyproject.toml',ROOT/'requirements-ci.txt',ROOT/'requirements.lock',ROOT/'requirements-quality.lock',
           *[ROOT/'scripts'/name for name in ['semantic_oracle.py','semantic_harness.py','reference_intent_plans.py','evaluate_intents.py']],
           *sorted(p for p in CORPUS.glob('*.json') if p!=MANIFEST)]
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def verify_manifest():
    manifest=json.loads(MANIFEST.read_text())
    if manifest['files']!=source_hashes():raise ValueError('Source/corpus/runner differs from manifest; deliberately refresh after review')
    return fingerprint(manifest)


class Replay:
    """No positional guessing or legacy action adaptation on changed requests."""
    def __init__(self,entries):self.entries=iter(entries);self.used=0
    def __call__(self,body):
        try:entry=next(self.entries)
        except StopIteration:raise ValueError('Replay exhausted') from None
        if entry['request_sha256']!=fingerprint(body):raise ValueError('Replay request mismatch')
        self.used+=1;return entry['response']
    def finish(self):
        if next(self.entries,None) is not None:raise ValueError('Unused replay responses')


def validate():
    ids=[];counts={}
    for split in ['development','candidate-holdout']:
        fixtures=json.loads((CORPUS/('fixtures.json' if split=='development' else 'candidate-fixtures.json')).read_text())
        cases=json.loads((CORPUS/(split+'.json')).read_text());counts[split]=len(cases)
        for case in cases:
            ids.append(case['id'])
            for turn in case['turns']:
                if turn['gold']['kind']=='query':expected_rows(fixtures,turn['gold'])
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate scenario IDs')
    return {'scenarios':counts,'gold_review':'candidate labels; independent human review pending'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--validate',action='store_true');mode.add_argument('--write-manifest',action='store_true')
    mode.add_argument('--reference',action='store_true');mode.add_argument('--replay',type=Path)
    parser.add_argument('--split',choices=['development','candidate-holdout'],default='development')
    parser.add_argument('--output',type=Path);parser.add_argument('--repetitions',type=int,default=1)
    args=parser.parse_args()
    try:
        validation=validate()
        if args.write_manifest:
            MANIFEST.write_text(json.dumps({'version':2,'label_provenance':'synthetic agent-authored; human review pending',
                'candidate_holdout':'not independently adjudicated or a fresh-model evaluation',
                'files':source_hashes()},indent=2)+'\n');print(json.dumps(validation));return
        frozen=verify_manifest()
        if args.validate:print(json.dumps({**validation,'manifest_sha256':frozen},indent=2));return
        if args.repetitions<1 or args.repetitions>20:raise ValueError('Repetitions must be 1..20')
        if args.output is None or args.output.resolve().is_relative_to(ROOT):raise ValueError('Output must be outside the public checkout')
        from semantic_harness import run_case,AttemptFailure
        from reference_intent_plans import ReferenceModel
        fixtures=json.loads((CORPUS/('fixtures.json' if args.split=='development' else 'candidate-fixtures.json')).read_text());cases=json.loads((CORPUS/(args.split+'.json')).read_text())
        plans=json.loads((CORPUS/'reference-plans.json').read_text());tapes=json.loads(args.replay.read_text()) if args.replay else None
        if tapes is not None and tapes['manifest_sha256']!=frozen:raise ValueError('Replay manifest mismatch')
        attempts=[]
        for repeat in range(args.repetitions):
            for case in cases:
                model=ReferenceModel(plans[case['id']]) if args.reference else Replay(tapes['attempts'][str(repeat)+':'+case['id']])
                result={'scenario_id':case['id'],'turns':[]}
                try:
                    result=run_case(case,fixtures,model)
                    if isinstance(model,Replay):model.finish()
                except AttemptFailure as error:
                    result={**error.partial,'status':'failed','failure_kind':error.kind}
                except (RuntimeError,ValueError,KeyError,IndexError) as error:
                    result={**result,'status':'failed','failure_kind':type(error).__name__}
                attempts.append({**result,'repetition':repeat+1})
        report={'mode':'scripted_control' if args.reference else 'recorded_replay','split':args.split,'manifest_sha256':frozen,
            'summary':aggregate(attempts),'attempts':attempts,
            'environment':{'python':sys.version,'packages':{name:version(name) for name in ['fastapi','httpx','pydantic','sqlglot']}},
            'limitations':['No fresh model inference or production latency claim','Gold labels and answer quality await independent human review','Outcome equivalence on fixtures does not prove arbitrary SQL semantics','No hidden retries; human semantic/style checks are pending']}
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:report[k] for k in ['mode','split','manifest_sha256','summary']},ensure_ascii=False,indent=2))
        return 1 if report['summary']['failed_attempts'] or any(v.get('fail',0) for v in report['summary'].values() if isinstance(v,dict)) else 0
    except (ValueError,OSError,KeyError,IndexError,RuntimeError) as error:
        parser.exit(2,'Evaluation failed ('+type(error).__name__+'); no request or source payload was printed.\n')


if __name__=='__main__':raise SystemExit(main())

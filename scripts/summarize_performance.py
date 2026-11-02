#!/usr/bin/env python3
"""Read an existing JSON array or JSONL export, print aggregate timings only."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from line_assistant.performance import summarize,markdown


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file',type=Path,help='Local action-trace/Cloud Logging JSON or JSONL export')
    parser.add_argument('--include-replays',action='store_true')
    parser.add_argument('--revision')
    parser.add_argument('--workflow')
    parser.add_argument('--boundary',choices=['assistant','webhook','interrupted'])
    parser.add_argument('--format',choices=['json','markdown'],default='json')
    args=parser.parse_args()
    try:
        raw=args.file.read_text()
        try:records=json.loads(raw)
        except ValueError:records=[json.loads(line) for line in raw.splitlines() if line.strip()]
        if isinstance(records,dict):records=records.get('entries')
        if not isinstance(records,list):raise ValueError()
        report=summarize(records,include_replays=args.include_replays,revision=args.revision,workflow=args.workflow,boundary=args.boundary)
    except (OSError,ValueError,TypeError,AttributeError):
        parser.exit(2,'Invalid local trace export; no source content was printed.\n')
    print(markdown(report) if args.format=='markdown' else json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

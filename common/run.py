"""One command for every domain/task: generate, extract, build-skills, evaluate, verify or inspect."""
import argparse
import json
from pathlib import Path
from common.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['generate', 'extract', 'build-skills', 'evaluate', 'verify', 'inspect'])
    parser.add_argument('--domain', choices=['finance', 'medical', 'law'], required=True)
    parser.add_argument('--task', choices=['dtt', 'rewriting', 'summary'], help='required for every stage except build-skills')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--execute', action='store_true', help='Call the model API; default only previews the plan and prompts')
    parser.add_argument('--limit', type=int, help='Number of rows (generate: TEST, extract: TRAIN, evaluate: TEST)')
    parser.add_argument('--output', type=Path, help='Run folder or file (default under runs/<domain>/...)')
    parser.add_argument('--model', help='models key or model ID (default: defaults.model)')
    parser.add_argument('--workers', type=int)
    parser.add_argument('--skills', type=Path, help='evaluate: domain catalog or task bank to use instead of skills/<domain>.json')
    parser.add_argument('--attach', action='store_true', help='generate: write outputs into data/<dataset>/test.json as llm')
    args = parser.parse_args()
    config = load_config(args.config)
    if args.stage == 'build-skills':
        from common.pipeline.extract import build_skills
        build_skills(config, args.domain, output=args.output)
        return
    if not args.task:
        parser.error(f'--task is required for {args.stage}')
    dataset, spec = next((name, spec) for name, spec in config['datasets'].items()
                         if spec['domain'] == args.domain and spec['task'] == args.task)
    if args.stage == 'inspect':
        print(json.dumps(spec, ensure_ascii=False, indent=2))
    elif args.stage == 'generate':
        from common.generation import generate
        generate(config, dataset, execute=args.execute, limit=args.limit, output=args.output, attach=args.attach)
    elif args.stage == 'extract':
        from common.pipeline.extract import run
        run(config, dataset, execute=args.execute, limit=args.limit, output=args.output, model=args.model, workers=args.workers)
    elif args.stage == 'evaluate':
        from common.pipeline.evaluate import run
        run(config, dataset, execute=args.execute, limit=args.limit, output=args.output, model=args.model,
            workers=args.workers, skills=args.skills)
    else:
        from common.pipeline.verify import run
        run(config, dataset, output=args.output)


if __name__ == '__main__':
    main()

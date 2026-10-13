# Evaluation pipeline skeleton

The shared skeleton of a human-vs-LLM evaluation pipeline: a frozen run layout, a cost gate with dry-run by
default, a resumable batched judge-call loop with a usage ledger, and win/tie aggregation with a sign test.
Evaluators (G-Eval, CheckEval, EvalAgent, neural metrics, ...) are not included; they plug into the stages below.

## Layout
```
pipeline/common.py          run context (Ctx), model routing and price table, cost gate, batched judge calls, ledger
pipeline/request_slots.py   shared rate limiting across processes
pipeline/results.py         frozen-run IO, pairing of human/LLM candidates, win/tie summary and sign test
examples/skeleton_demo.py   offline walk through the stages below
examples/reversal/          two judgment-reversal examples
```

## Stages
```
runs/<dataset>/<version>/
  split/split.json       frozen evaluation cases: input, source units, human reference (gt), meta
  generate/llm.jsonl     model output (llm) per case
  llm/<method>/          raw judge outputs (resumable jsonl), summary.json, per_case.csv
  neural/<metric>/       non-LLM metric scores, summary.json, per_case.csv
```
1. **Freeze** inputs and model outputs (`results.write_same` never overwrites a changed file).
2. **Pair** the two candidates of every case (`results.load_cases`).
3. **Gate** the calls: `common.gate` prints pending calls and the estimated cost and sends nothing unless `--yes`.
4. **Run** the pending jobs with `common.run_batch` (concurrency, retries, resume by job key); each batch appends
   to `logs/api_usage_ledger.jsonl`.
5. **Aggregate** per-side scores with `results.summarize`: higher score wins, equality is a tie, exact sign test
   over non-tied pairs. Win rates are wins over all pairs, ties kept in the denominator.

## Run
```bash
pip install -r requirements.txt
python examples/skeleton_demo.py            # offline: freeze, pair, gate (dry run), aggregate a toy score
python examples/reversal/show_reversal.py   # two reversal examples
```
For live (`--yes`) runs, create a `.env` file with `OPENAI_API_KEY` and/or `OPENROUTER_API_KEY`.

## Judgment-reversal examples
`examples/reversal/reversal_examples.json` holds two EPA pairs in which all seven judges, and the gpt6-luna
pairwise judge in both presentation orders, prefer the LLM text over the human-written reference. See
`DATA_SOURCES.md` for the source and a caveat on reference quality.

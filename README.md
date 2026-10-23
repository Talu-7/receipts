# Receipts

**An agent that checks whether a paper's headline result actually reproduces.**

Paste an arXiv link. Receipts extracts the headline claim, finds the code with web search, runs the
reproduction over many seeds in a sandbox, and returns one of four verdicts, **reproduced**,
**partially reproduced**, **discrepancy found** or **inconclusive**, together with a *receipt*: the executed
code, the per-seed numbers, the logs, a tamper-evident hash, and a one-command way to re-run it.

Built for the **Nebius × NVIDIA Global AI Hackathon**.

- **Demo (recorded replays):** https://YOUR-USERNAME.github.io/receipts/
- **Video:** add your YouTube link here

## How it uses NVIDIA Nemotron on Nebius Token Factory

Every model call in the product goes through **Nebius Token Factory's OpenAI-compatible API** to an
**NVIDIA Nemotron 3** model. Each model does the job it is best suited to:

| Stage | Model | What it does |
|---|---|---|
| Claim extraction | **Nemotron 3 Super** | Reads the paper and picks the headline number that can actually be re-run |
| Code generation + repair | **Nemotron 3 Super** | Writes the reproduction script, and rewrites it from the error log if the first run fails |
| Log summaries, evidence filtering | **Nemotron 3 Nano** | Cheap and fast: summarises job logs, and filters search hits down to pages that really report an erratum or a replication of *this* paper |
| Diagnosis | **Nemotron 3 Ultra** | Reasons about *why* a number differs and names one cause (leakage, metric definition, hyperparameters...), citing the numbers and any computed variant that matches the claim |

Implementation notes (`receipts/llm.py`): model IDs are auto-resolved from `/v1/models`; Nemotron 3 models
reason before answering, so each call gets headroom for reasoning tokens and is retried once with a doubled
budget if the answer comes back empty; `<think>` blocks are stripped and JSON is extracted robustly.

**Tavily** (`receipts/search.py`, `receipts/discover.py`) finds the code repository, datasets, errata and prior
replication attempts at runtime.

## Why you can trust a verdict

- **Statistics, not one run.** The claim is compared with the distribution over N seeds. Reproduced if the mean
  is no worse than the claim by more than `max(tolerance, 2σ)`; partial up to `max(3·tolerance, 4σ)`; otherwise a discrepancy.
- **"Inconclusive" is a first-class verdict.** If fewer than half the runs produce a number, we say so instead
  of accusing the paper because our environment broke. Claims too expensive to re-run are skipped up front.
- **A shrunken run can never count against a paper.** If the script declares it ran at reduced scale (fewer
  steps, a subset, a smaller model), a bad number is downgraded to inconclusive.
- **No cheating.** Generated scripts that contain the claimed number as a literal are rejected.
- **Honest evidence.** Search results are only shown as errata/replications if Nano judges that they really are.
- **Receipts are tamper-evident.** `python -m receipts verify receipt.json` checks the SHA-256 and then re-runs
  every seed and compares with the recorded numbers.
- **Sandboxed.** Runs happen in a container with all capabilities dropped, no-new-privileges, and pid/memory/CPU
  limits. This is not a perfect security boundary (shared kernel). Isolation requests fail closed.

## Measured on known answers

`bench/planted.json` holds eight synthetic cases with a known right answer: four planted bugs (feature selection
before cross-validation, a hyperparameter tuned on the test set, training accuracy reported as test accuracy,
duplicate rows across the split), one mild overstatement, two clean controls, and one proprietary-data case that must be
*inconclusive*.

| Metric | Result |
|---|---|
| Verdicts matching the known answer | 8 / 8 |
| Planted errors detected | 4 / 4 |
| Clean cases wrongly flagged | 0 / 2 |

Verdicts come from deterministic statistics. The cause-attribution score depends on which model wrote the
diagnosis, so re-run it with your Token Factory key: `python -m receipts bench bench/planted.json --seeds 8`.
These cases are synthetic by design (they measure the method, not real papers) and are labelled as such everywhere.

## Quickstart

```bash
cp .env.example .env            # add NEBIUS_API_KEY and TAVILY_API_KEY
python -m receipts doctor       # checks keys, resolves the three Nemotron models
python -m receipts serve        # web UI at http://127.0.0.1:8000
python -m receipts run demo:leakage --seeds 8              # offline, no keys needed
python -m receipts run https://arxiv.org/abs/XXXX.XXXXX --runner docker --seeds 10
python -m receipts verify out/receipt-<id>.json
python -m unittest discover -s tests
```

The core uses only the Python standard library.

## Runners

| `--runner` | Where code runs | Notes |
|---|---|---|
| `local` | your Python, no container | fast for the bundled demos; do not use on untrusted repos |
| `docker` | the sandbox image (`sandbox/Dockerfile`) via `docker run` | hardened; network off when nothing must be downloaded |
| `nebius` | Nebius Serverless Jobs (`nebius ai job create`) | one job per seed, in parallel; same image as `docker` |

**Honest status:** the Nebius Serverless Jobs runner is implemented against the CLI reference, but our hackathon
account could not create jobs (`PermissionDenied`, including on a `--dry-run`), so it has not been exercised
end to end. The Docker runner uses the identical image and entrypoint. All inference runs on Token Factory.

## Known limits

- Scope: papers with a public repo and a headline number re-runnable in roughly an hour or less. Others come out
  *inconclusive* with the reason, e.g. a 2017 TensorFlow 1.x repo that cannot be installed today.
- A discrepancy is a finding under stated conditions (seeds, code version, environment), not an accusation.
- Run untrusted repositories only in the Docker or Nebius runner.

## Project layout

`receipts/` the agent (pipeline, claims, discover, codegen, diagnose, stats, runner, server) ·
`sandbox/Dockerfile` the job image · `static/` UI and replay-site template · `bench/` known-answer cases ·
`docs/` the static replay site · `tests/` 37 tests.

## License

MIT, see `LICENSE`.

# Text-to-SQL: SFT vs. RL Post-training at 3B Scale

This project compares supervised fine-tuning (SFT) and reinforcement learning (GRPO) as
post-training methods for text-to-SQL generation on a single consumer GPU with 8GB VRAM. Starting
from Qwen2.5-Coder-3B-Instruct, we train one QLoRA SFT model and two GRPO RL models on the same
Spider training subset, and evaluate them in-distribution on Spider eval and out-of-distribution
on BIRD eval. We also use a per-category error taxonomy to study which types of errors each
method actually reduces.

The full write-up, including the results, error analysis, and case studies, is available at
[this page](https://levi-hsu.github.io/blog/2026/text-to-sql/).

## Main results

| models | Spider eval EX | BIRD eval EX | Spider → BIRD drop |
|---|---|---|---|
| baseline (zero-shot) | 0.6083 | 0.2073 | 0.4010 |
| SFT | 0.6973 | 0.1877 | 0.5095 |
| RL | 0.6973 | 0.1956 | 0.5017 |
| RL-v2 | 0.7128 | 0.1851 | 0.5276 |

Post-training clearly improves in-distribution accuracy, but every post-trained arm generalizes
*worse* than the zero-shot baseline. See `blog/blog.md` for the error analysis and a
more detailed explanation of this result.

## What's in this repo

```
configs/    YAML configs for every arm (baseline, SFT, RL, RL-v2, BIRD-adapt variants, evals)
scripts/    Data prep, training, evaluation, error-taxonomy, and figure-generation code
blog/       Write-up (blog.md) and figures used in it
data/       Small, checked-in JSONL files: the exact filtered training/eval subsets used
runs/       Training/eval outputs -- NOT checked in, see below
```

## What is *not* in this repo

**Model weights are not pushed to GitHub.** This includes:

- The base model, Qwen2.5-Coder-3B-Instruct (bf16, ~6GB) — download it yourself from Hugging
  Face (`Qwen/Qwen2.5-Coder-3B-Instruct`) and point `model.name_or_path` in the relevant config
  file at your local copy.
- All trained LoRA adapters and checkpoints under `runs/` (SFT, RL, RL-v2, and the BIRD-adapt
  variants) — `runs/` is ~16GB across all models and is gitignored in full. Every adapter is fully
  reproducible from the configs and scripts in this repo (see below); the exact command for each
  arm is documented in `blog/blog.md`'s Reproducibility section.
- Raw Spider and BIRD data under `data/spider_data/` and `data/bird-dev/` (~1.8GB each) and
  `spider.zip`. These are third-party datasets with their own distribution terms — download them
  from the official sources rather than from this repo:
  - Spider: [yale-lily.github.io/spider](https://yale-lily.github.io/spider) or the
    `xlangai/spider` dataset on Hugging Face
  - BIRD: [bird-bench.github.io](https://bird-bench.github.io)

  The small JSONL files under `data/sft/` and `data/bird_adapt/` *are* checked in. These are the
  filtered or derived subsets actually used for training, rather than the raw datasets. They are
  only a few MB, so keeping them in the repository also makes the exact training data easy to
  inspect.

If you want to provide a runnable adapter without requiring retraining, the simplest option is
to upload the specific adapter directory you need (e.g. `runs/sft_qwen2.5coder3b/adapter`,
~126MB) to the [Hugging Face Hub](https://huggingface.co/new) and link it here. GitHub is not
well suited for binary model artifacts, even at this size.

## Setup

```bash
pip install -r requirements.txt
```

`torch` is intentionally excluded from `requirements.txt`. Install the CUDA build that matches
your driver first (check `nvidia-smi`), following the instructions at
[pytorch.org](https://pytorch.org).

The experiments were developed and run on an RTX 4060 Laptop GPU with 8GB VRAM, 16GB system
RAM, and no swap. QLoRA, with 4-bit NF4 base weights and bf16 LoRA adapters, keeps every arm
within this hardware budget.

## Reproducing the results

Each script should be run from the project root:

```bash
bash scripts/run_baseline_eval.sh   # zero-shot baseline, Spider eval + BIRD eval
bash scripts/run_sft.sh             # QLoRA SFT arm: train + evaluate
bash scripts/run_rl_eval.sh         # RL arm (GRPO from the SFT checkpoint)
bash scripts/run_rl_eval_v2.sh      # RL-v2 arm (more rollouts + partial-credit reward)
bash scripts/run_bird_eval.sh       # scores all models' existing checkpoints on BIRD eval
bash scripts/run_experiment2_chain.sh        # BIRD-continue adaptation experiment
bash scripts/run_rl_algo_variants_chain.sh   # RLOO / Dr. GRPO algorithm-swap replicates
bash scripts/run_blog_artifacts.sh  # regenerates every table and figure cited in blog/blog.md
```

Each run writes its outputs, including the adapter, predictions, scorecards, and health logs, to
`runs/<arm-name>/`.

## Limitations

The training data used here is small relative to the large-scale studies this project is based on
(SQL-R1, Reasoning-SQL, and Arctic-Text2SQL-R1), so the absolute accuracy numbers should not be
compared directly with their published results. The generalization analysis also uses only one
OOD dataset, BIRD eval. See `blog/blog.md` for a more complete discussion of these limitations.

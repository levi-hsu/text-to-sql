# Text-to-SQL Project Plan

## Overall goal

Fine-tune a small open-source LLM for text-to-SQL on a single consumer machine, and use that resource-constrained setting to run a controlled study of whether RL post-training generalizes across unseen database schemas better than supervised fine-tuning alone. The hardware constraint is not just a limitation to work around, it is part of the experimental design: every result is reported against a documented, fixed compute budget.

**Hardware/platform, updated 2026-08-08:** training moved from a Mac M2 (MLX) to a Windows 11 machine with an NVIDIA GPU, 8-12GB VRAM, because Mac generation/training speed was too slow for practical iteration. This is a full platform switch, not a portability tweak: MLX is Apple Silicon-only by design (no Windows or generic-CUDA build exists), so the entire training stack changed along with it, from `mlx_lm_lora.train` to `transformers` + `peft` + `trl` + `bitsandbytes` (see Training infrastructure below). The compute-budget discipline stays the same in spirit, just re-anchored to the new hardware: 8-12GB VRAM means 4-bit (QLoRA-style) loading is required, not optional, for the 7B model to fit at all.

## Research question

Under a fixed compute budget on a single consumer machine, does RL post-training, with an execution-accuracy reward, produce better cross-schema generalization than LoRA supervised fine-tuning alone, and how does that RL-vs-SFT generalization gap depend on how many distinct database schemas the SFT warm-start stage was exposed to.

This supersedes the earlier LoRA-rank-only framing. Rank is now a secondary, fixed-unless-time-allows factor; the primary comparison is training method (SFT-only vs. SFT+RL) crossed with schema diversity (single-database vs. many-database warm-start). Two RL algorithms are tested for the RL arm of that comparison -- GRPO and PPO -- see "RL algorithms tested" below. (TRPO was considered for its stronger theoretical guarantee -- see the 2026-08-08 log entries for the analysis -- but dropped for this round to keep the run count small; nothing about that analysis was wrong, it's a scope decision, and TRPO stays a candidate extension if GRPO/PPO leave an open question their comparison alone can't answer.)

## Expected outcome and what counts as a result

This project takes no side on whether RL will outperform SFT, and a negative or null result is a legitimate, reportable outcome, not a failed run. The literature is genuinely split on this exact question:

- "SFT Memorizes, RL Generalizes: A Comparative Study of Foundation Model Post-training" (Chu et al., ICML 2025) reports that RL trained with an outcome-based reward generalizes to out-of-distribution rule variants that SFT overfits to, in both textual and visual domains -- directly analogous to this project's cross-schema generalization question, and the paper this project's RQ is closest to in spirit. It also reports that SFT remains necessary before RL: SFT stabilizes output format, which is what makes the subsequent RL stage work at all, matching this project's Stage 1-then-Stage 2 structure.
- "Does Reinforcement Learning Really Incentivize Reasoning Capacity in LLMs Beyond the Base Model?" (Yue et al., 2025, NeurIPS 2025 Best Paper Runner-up) found that RLVR training narrows the sampling distribution toward capabilities already present in the base or SFT model rather than creating new capacity -- at large sampling budgets (pass@k for large k), the base or SFT model can match or exceed the RL-trained model's accuracy. Applied here, this predicts RL may sharpen the SFT model's best-guess accuracy (k=1, which is what EX on a single generated query measures) without expanding what the model is fundamentally capable of producing.

Both are high-confidence readings of real, peer-reviewed papers; which pattern holds for this project's specific setup (7B LoRA-adapted model, Spider schemas, small compute budget) is exactly what Stage 1 vs. Stage 2 evaluation is designed to find out. If RL ties or loses to SFT here, the useful finding is characterizing where and why, not treating the run as void.

## Why RL fits this task

Execution accuracy (EX) is a program-checkable reward: run the generated SQL, diff the result table against the gold query's result table, no LLM judge or human label required. This execution-verifiable-reward property is what makes any mainstream policy-gradient RL algorithm cheap to run here, since the reward function needs no learned reward model. It is also the load-bearing design choice behind recent small-model text-to-SQL RL work — Arctic-Text2SQL-R1 (execution-only reward, GRPO, 7B, top of the BIRD leaderboard at release), Reasoning-SQL (GRPO with partial-credit rewards to address reward sparsity), and CSC-SQL (GRPO plus self-consistency, 71.7% BIRD test EX at 7B) — all of which use SFT warm-start followed by RL with an execution-based reward, at model scales matching or smaller than this project's base model. High confidence these are real, relevant precedents; medium confidence they transfer cleanly to an 8-12GB-VRAM compute budget, since none of them were run under that specific constraint (though the move to a CUDA GPU makes this comparison more standard than it was on MLX, since GRPO/PPO on transformers+peft+trl is the exact setup those papers themselves used).

### RL algorithms tested

Two policy-gradient algorithms are run against each other:

- GRPO. Critic-free, group-normalized advantage. No formal monotonic-improvement or global-convergence guarantee of its own; it is an empirically-motivated simplification of PPO's clipped surrogate for the case where multiple rollouts per prompt are cheap to sample. The default, since `mlx-lm-lora` implements it natively with a straightforward path to a custom execution-accuracy reward function (see Training infrastructure below).
- PPO. The standard critic-based policy-gradient method GRPO was designed to simplify, and the more established of the two. No formal guarantee either, but it's the direct, widely-used point of comparison for GRPO in the literature (Arctic-Text2SQL-R1, Reasoning-SQL, CSC-SQL, and most RLHF-style LLM post-training all frame themselves against PPO).

TRPO (Trust Region Policy Optimization) was considered as a third candidate specifically for its proven monotonic-improvement guarantee (Schulman et al., 2015, arXiv:1502.05477, building on Kakade and Langford, 2002; the broader theory is in Agarwal, Kakade, Lee, and Mahajan, JMLR 22(98), 2021, arXiv:1908.00261). Dropped for this round to keep the total RL run count small -- not because the theoretical argument was wrong, and not a closed door: if the GRPO-vs-PPO comparison itself turns out ambiguous or surprising, TRPO (or its cheaper adaptive-KL-penalty PPO variant, Schulman et al. 2017, arXiv:1707.06347) is the documented next thing to try, and the reasoning for why LoRA might make TRPO's usually-prohibitive conjugate-gradient cost tractable here is preserved in the 2026-08-08 log entries rather than deleted.

Whichever algorithm, the reward function, evaluation harness, and Stage 1 SFT checkpoints stay identical; only the RL update rule changes. Log which algorithm was used for each run, and its wall-clock and peak-memory cost.

**Open implementation question, carried over from the Mac tooling, status changed by the Windows move:** `mlx-lm-lora`'s GRPO mode took a custom reward function directly, which fit this project's execution-accuracy reward cleanly; its PPO mode was built around `--judge <reward_model_id>`, a learned reward model, which didn't fit an execution-verifiable reward without a wrapper. That specific blocker goes away with the platform switch: `trl`'s `GRPOTrainer` (the Windows/CUDA equivalent) also takes a `reward_funcs` argument as a list of plain Python callables, and `trl`'s `PPOTrainer` computes rewards in the caller's own training loop rather than requiring a loaded reward-model object, so an execution-accuracy callable plugs into both without a stand-in judge model. Not independently verified end to end yet (Stage 2 hasn't been reached), but the shape of the problem is better under `trl` than it was under `mlx-lm-lora`. Not a blocker for Stage 1 SFT, which doesn't touch this at all.

## Experimental design

### Baseline evaluation

Before any fine-tuning, evaluate the base model zero-shot or few-shot on Spider dev, with the schema given in context. This establishes the starting point that both SFT and SFT+RL are measured against.

### Stage 1, SFT warm-start

Fine-tune with LoRA on Spider under two schema-diversity conditions: schema-agnostic (many distinct training databases) and schema-specific (one fixed database). This produces two SFT checkpoints, matching the old Experiment A and Experiment B. SFT warm-start before RL is not optional at this model scale: without it, reward groups risk coming back all-zero regardless of which RL algorithm is used (see reward design below), and prior small-model text-to-SQL RL work uniformly starts from an SFT checkpoint rather than the raw base model.

Both conditions train with early stopping on validation loss (patience-based, via `transformers.EarlyStoppingCallback`), rather than a fixed iteration count decided in advance. This wasn't available in the Mac/MLX setup (`mlx_lm_lora.train` had no equivalent flag) and is one of the concrete things the Windows/CUDA move enables; it's a genuinely better fit than a fixed iters count for the schema-specific condition in particular, since Experiment B's small dataset (108 train examples) makes the right stopping point hard to guess correctly in advance and easy to either underfit or overfit past.

### Stage 2, RL post-training

Starting from each of the two Stage 1 checkpoints (schema-agnostic and schema-specific), run both GRPO and PPO, sampling rollouts from the same schema pool the corresponding SFT stage used. This is a full 2 (algorithm) x 2 (diversity) cross, four RL training runs total from the two SFT starting points, isolating algorithm and diversity as independent factors.

### Evaluation

Evaluate all checkpoints -- base, SFT-narrow, SFT-diverse, SFT+GRPO-narrow, SFT+GRPO-diverse, SFT+PPO-narrow, SFT+PPO-diverse -- on both in-domain held-out questions (same schemas seen in training) and Spider's disjoint, held-out test databases (schemas never seen in training). Two things come out of this: the training-method x schema-diversity comparison (SFT vs. SFT+RL, narrow vs. diverse) and the algorithm comparison (GRPO vs. PPO, at each diversity level), reported separately for in-domain accuracy and held-out generalization accuracy.

### Reward design

Primary reward: binary execution accuracy, no reward shaping. This follows Arctic-Text2SQL-R1's own finding that a simple reward suffices at this model scale, and avoids the reward-hacking risk of shaped rewards. Fallback, if the SFT-warm-started model's EX is low enough that most reward groups/batches come back all-zero (no learning signal, a risk under GRPO, RLOO, and rejection-sampling alike): switch to partial-credit rewards combining schema-linking F1, syntax validity, and EX, following Reasoning-SQL's approach to the same reward-sparsity problem. Decide which regime applies empirically, after Stage 1 SFT checkpoints exist and their EX is measured.

### Varying factors

- Training method: SFT-only vs. SFT+RL. Primary axis.
- Schema diversity: single-database vs. many-database warm-start and RL rollout pool. Primary axis.
- LoRA rank: held fixed (for example 16) unless compute allows a secondary sweep once the primary 2x2 is done.
- Training data volume: held fixed at Stage 1's full available set unless compute forces a reduction.
- RL algorithm: GRPO vs. PPO, crossed fully with schema diversity (see Stage 2). Primary axis alongside training method and schema diversity.

### Compute budget and environment

Every run is logged with: base model and quantization level, LoRA rank, training set size, RL algorithm, group/batch size and rollout count, wall-clock time, and peak GPU memory. RL is materially more expensive per step than SFT, since each step requires generating multiple rollouts per prompt (typically 4-16) rather than a single forward-backward pass, and PPO adds its own per-step cost on top of GRPO's for the value/critic network. Before committing to the full four-run RL grid, run a short pilot per algorithm, a few dozen steps at a small group size, to measure wall-clock per step and confirm each fits the documented budget and the 8-12GB VRAM ceiling.

## Deliverable

One comparison table and generalization-gap plot, now with algorithm as an explicit column: base model with no fine-tuning, SFT-only (narrow and diverse), SFT+GRPO (narrow and diverse), SFT+PPO (narrow and diverse), and the specialized reference checkpoints, evaluated on both in-domain and held-out Spider databases. The headline figures are the RL-vs-SFT generalization gap (held-out accuracy minus in-domain accuracy, or held-out accuracy alone) and the GRPO-vs-PPO comparison at matched schema diversity, both reported honestly whichever direction they point, alongside each run's measured wall-clock and peak-memory cost.

## Models

See log.md for the running list of which model is used for which run.

- Qwen2.5-Coder-7B-Instruct, primary fine-tuning base. Two copies now exist for two different platforms and are NOT interchangeable: an MLX 4-bit checkpoint at `/Users/lerong/llm-models/Qwen2.5-Coder-7B-Instruct-4bit` (Mac-only, MLX's own quantization format, unreadable by transformers/bitsandbytes) and the plain HF Hub checkpoint `Qwen/Qwen2.5-Coder-7B-Instruct` used on Windows, loaded in 4-bit at runtime via bitsandbytes (`BitsAndBytesConfig(load_in_4bit=True, ...)`) rather than pre-quantized on disk. The Windows configs (`configs/*.yaml`) reference the HF Hub id directly since I don't have visibility into the Windows machine's filesystem; point them at a local path instead if the model is already downloaded there.
- Qwen2.5-Coder-3B-Instruct, fallback if memory is tight, and a second point on the model-size axis if useful later. Not yet downloaded on either platform.
- OmniSQL-7B, reference checkpoint, already fine-tuned for text-to-SQL at scale. Not yet downloaded.
- Arctic-Text2SQL-R1-7B, reference checkpoint, RL-trained for text-to-SQL. Not yet downloaded.

Also present locally but not part of the current plan: `/Users/lerong/llm-models/Qwen3-1.7B-4bit`, a leftover from the earlier Countdown project's model choice, before this project moved to Qwen2.5-Coder-7B-Instruct. Not used here.

## Data

Spider is downloaded and extracted to `/Users/lerong/Documents/text-to-sql/spider_data/` (tables.json, train_spider.json, train_others.json, dev.json, plus the per-database sqlite files) on the Mac. `scripts/prepare_spider_data.py` builds the actual SFT/eval files from this into `/Users/lerong/Documents/text-to-sql/data/`: `sft_diverse/{train,valid}.jsonl` (8313/346 examples, 146 databases, schema-agnostic condition), `sft_narrow/{train,valid}.jsonl` (108/11 examples, single database `college_2`, schema-specific condition), `eval/dev.jsonl` (1034 examples, 20 databases, disjoint from all training data -- confirmed zero db_id overlap), and `eval/narrow_heldout.jsonl` (51 examples, same db as sft_narrow, held out, for in-domain eval of the schema-specific condition). This JSONL data format (`{"messages": [...]}` per line) is platform-independent and unchanged by the Windows move -- `trl`'s `SFTTrainer` reads it directly. It needs to physically exist on the Windows machine too (either copy `spider_data/` and `data/` over, or re-run `prepare_spider_data.py` there against a fresh Spider download); nothing about the data pipeline itself changed. See log.md for how these numbers were produced and verified.

## Training infrastructure

Changed 2026-08-08 along with the Mac-to-Windows move (see Overall goal). SFT, GRPO, and PPO now run through `scripts/train.py` (SFT) and TRL's `GRPOTrainer`/`PPOTrainer` (RL, Stage 2, not yet built), on top of `transformers` + `peft` + `trl` + `bitsandbytes` -- the standard CUDA-native stack, replacing `mlx-lm-lora`. Config-driven, one YAML file per run under `configs/`, matching the same "one command, one config" workflow `mlx_lm_lora.train --config ...` had: `python scripts/train.py --config configs/sft_diverse_rank16.yaml`. LoRA via `peft.LoraConfig`, 4-bit base weights via `bitsandbytes.BitsAndBytesConfig` (necessary, not optional, at 8-12GB VRAM for a 7B model), loss masked to the assistant turn via `trl.SFTConfig`'s completion-only-loss option (exact field name is version-sensitive across `trl` releases; `scripts/train.py` tries the current name and falls back to an older one, see its docstring), early stopping via `transformers.EarlyStoppingCallback` on eval loss (new capability, not available under `mlx_lm_lora.train`; see Stage 1 above). Data format, schema linearization, and the EX verifier/reward function (`scripts/eval_sql.py`) are all unchanged from the Mac version -- only the model-loading and training-loop layer changed. `scripts/eval_sql.py`'s query-timeout mechanism was also fixed as part of this move: it used `signal.SIGALRM`, which is POSIX-only and doesn't exist on Windows, and now uses a cross-platform `threading.Timer` + `sqlite3.Connection.interrupt()` instead.

## Scripts

All under `scripts/`, all platform-independent except `run_baseline_eval.py` and `train.py` (which import `torch`/`transformers`, so only run where those are installed -- Windows now, not this project's Linux/sandbox dev environment). Four are runnable entry points; `sql_prompt.py` is a shared library imported by two of the others, not run on its own.

- **`sql_prompt.py`** (shared module, not a standalone script). Schema linearization (Spider's `tables.json` entries into `CREATE TABLE` DDL with inline `PRIMARY KEY`/`FOREIGN KEY`) and the chat-message prompt builder. Imported by `prepare_spider_data.py` and `run_baseline_eval.py` so the SFT prompt and the inference prompt are built by the exact same code and can't drift apart.
- **`prepare_spider_data.py`**. Reads the raw Spider download (`spider_data/`) and writes the SFT/eval JSONL files (`data/sft_diverse/`, `data/sft_narrow/`, `data/eval/dev.jsonl`, `data/eval/narrow_heldout.jsonl`). Run once per machine that has a Spider download; already run on the Mac, still needs running on Windows (see Data above). No third-party dependencies (stdlib only), runs anywhere Python 3 does.
- **`eval_sql.py`**. The execution-accuracy (EX) verifier: runs gold and predicted SQL against the real sqlite file, compares result rows, cross-platform query timeout. Used both as a CLI (`python scripts/eval_sql.py --eval-file ... --pred-file ...`) and as a library (`execution_match()`, `score_file()`) imported by `run_baseline_eval.py`, and is also the function Stage 2's RL reward will call. No third-party dependencies (stdlib only: `sqlite3`, `threading`, `json`).
- **`run_baseline_eval.py`**. Loads a model (base, or base + LoRA adapter), generates SQL for every example in an eval JSONL, scores it with `eval_sql.py`. Requires `torch`, `transformers`, and, if evaluating a fine-tuned checkpoint, `peft`.
- **`train.py`**. Config-driven Stage 1 SFT: reads a YAML file (`configs/*.yaml`), loads the base model 4-bit via `bitsandbytes`, wraps it with a LoRA adapter via `peft`, trains with `trl.SFTTrainer` (assistant-turn-only loss, optional early stopping), saves the adapter. Requires `torch`, `transformers`, `peft`, `trl`, `bitsandbytes`, `datasets`, `pyyaml`. Entry point for both `configs/sft_diverse_rank16.yaml` and `configs/sft_narrow_rank16.yaml`.

## Environment / dependencies (Windows)

Everything needed to run `train.py` and `run_baseline_eval.py`. `prepare_spider_data.py`, `eval_sql.py`, and `sql_prompt.py` need nothing beyond the Python standard library.

- **`torch`** -- the underlying tensor/autograd library everything else is built on. Install the CUDA build matching your installed CUDA version (check `nvidia-smi` for the driver's supported CUDA version, then use the matching command from pytorch.org -- the pip index URL differs by CUDA version, e.g. `--index-url https://download.pytorch.org/whl/cu121` for CUDA 12.1, don't just `pip install torch` and assume it picks the right build).
- **`transformers`** -- model/tokenizer loading (`AutoModelForCausalLM`, `AutoTokenizer`, `BitsAndBytesConfig`), and the underlying `Trainer`/`EarlyStoppingCallback` that `trl.SFTTrainer` builds on.
- **`peft`** -- LoRA (`LoraConfig`, `get_peft_model`, `PeftModel.from_pretrained` for loading a trained adapter at eval time).
- **`trl`** -- `SFTTrainer`/`SFTConfig` now (Stage 1), `GRPOTrainer`/`PPOTrainer` later (Stage 2).
- **`bitsandbytes`** -- 4-bit (`load_in_4bit`) quantized model loading, required (not optional) to fit a 7B model in 8-12GB VRAM.
- **`datasets`** -- loads the `train.jsonl`/`valid.jsonl` files (`datasets.load_dataset("json", ...)`) into the format `SFTTrainer` expects.
- **`accelerate`** -- installed as a dependency of `transformers`/`trl`/`peft`'s multi-backend device handling (`device_map="auto"`); not imported directly by this project's scripts, but needs to be present.
- **`pyyaml`** -- reads the `configs/*.yaml` files (imported as `yaml` in `train.py`).
- **`numpy`** -- not imported directly anywhere in this project's own code, but is a hard dependency of `torch`/`transformers`/`datasets` and gets installed automatically with them; no separate action needed.

Install command:

```
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt
```

(swap the `cu121` index URL for whatever matches your actual CUDA version -- see above. `requirements.txt`, at the project root, pins the rest: `transformers`, `peft`, `trl`, `bitsandbytes`, `datasets`, `accelerate`, `pyyaml`. `torch` is deliberately left out of it and installed separately first, since the right command depends on your CUDA version and isn't a plain `pip install torch`.)

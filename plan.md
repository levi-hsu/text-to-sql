# Text-to-SQL: SFT vs RL Post-training

## Goal

The project compares supervised fine-tuning and reinforcement learning as post-training methods for text-to-SQL generation, under a controlled experimental design, on hardware limited to a single 8GB-VRAM consumer GPU.

The central question: given the same base model and the same training data budget, how much does each post-training method improve execution accuracy over a zero-shot baseline, and does one generalize better than the other to schemas and query styles not seen during training. A secondary question, addressed through error taxonomy analysis, is which failure modes each method actually fixes rather than only reporting aggregate accuracy.

This project is a scaled-down, controlled replication of findings reported in SQL-R1, Reasoning-SQL, and Arctic-Text2SQL-R1, which show RL post-training improving over SFT-only baselines by several execution-accuracy points at 7B-32B scale with large compute budgets. This project asks whether the same direction of effect holds at small scale with a matched, controlled setup, and adds an error-category breakdown that those papers do not report.

## Setting

### Model

Qwen2.5-Coder-3B-Instruct, downloaded as the original bf16 release from Hugging Face, is the single model used for the core comparison. All three experimental arms (zero-shot baseline, SFT, RL) use this exact model, so that architecture, parameter count, and tokenizer are held fixed and only the post-training procedure varies.

Training uses QLoRA: the frozen base weights are quantized to 4-bit NF4 at load time via bitsandbytes, with LoRA adapters trained in bf16 on top. This keeps weight storage at roughly 1.5-2GB, leaving VRAM headroom for gradients, optimizer state, KV cache, and, during the RL arm, multiple parallel rollouts per prompt.

If VRAM proves insufficient once rollout generation is added during RL training, Qwen2.5-Coder-1.5B-Instruct is the fallback, run through the identical three-arm design.

Qwen2.5-Coder-7B-Instruct-GPTQ-Int8, already available locally, is used only as a zero-shot inference reference point, evaluated but never trained. It is excluded from the controlled comparison because it differs from the 3B model in three confounded dimensions at once (parameter count, quantization level, training status), so any gap against it cannot be attributed to a single cause. Its results are reported separately from the core findings, labeled as context rather than a controlled baseline.

Rationale for model choice: Qwen2.5-Coder is the base model family used by SQL-R1, OmniSQL, Reasoning-SQL, and Arctic-Text2SQL-R1, so results at 3B scale remain comparable in kind, if not in magnitude, to that literature.

### Datasets

Spider (train and dev splits) is the primary training and in-distribution evaluation set. It is the same schema distribution used for training, so Spider-dev measures in-distribution accuracy. Spider-train is not yet downloaded; it needs to be pulled from the official Spider release (yale-lily.github.io/spider) or the `xlangai/spider` Hugging Face dataset before the SFT arm can start. Unlike BIRD-train, Spider-train is small (JSON annotations under 10MB, plus lightweight per-database SQLite files for the train-side databases), so downloading it is feasible on this machine.

BIRD-dev is the out-of-distribution evaluation set. It is not trained on. The gap between in-distribution (Spider-dev) and out-of-distribution (BIRD-dev) accuracy is the generalization measure for each arm.

BIRD-train is excluded from the project. Its per-database data files are large enough (multi-GB across its ~70 real-world databases) that downloading and hosting it is impractical on this setup. Because BIRD-train is not used, no BIRD-derived data enters training, so BIRD-dev remains a clean, untouched OOD set.

Spider-DK is dropped from the design; it was not downloaded and is not required for the core comparison. This narrows the project to a single OOD set (BIRD-dev) rather than two, so the generalization measure is now Spider-dev to BIRD-dev only, not averaged or compared across multiple OOD sets.

Given the compute budget, training uses a filtered subset of Spider-train rather than the full set, sized to keep SFT and RL training runs within a few GPU-hours each on the RTX 4060. The exact subset size is finalized after a first timing pass on the SFT arm.

### Hardware and tooling

RTX 4060 Laptop GPU, 8GB VRAM; 16GB system RAM, no swap configured. Training uses Hugging Face Transformers, PEFT for LoRA/QLoRA, bitsandbytes for 4-bit quantization, and TRL or Unsloth for the GRPO implementation used in the RL arm. vLLM is used for rollout generation during RL if throughput requires it.

Without swap, a CPU-side memory spike (large dataloader batches, too many worker processes, or a large rollout buffer during RL) can hard-crash the process instead of degrading gracefully. Mitigations: keep `num_workers` low, avoid loading the full dataset into RAM at once, and monitor `free -h` during the first runs of each arm.

## Experimental design

### Arms

Baseline: Qwen2.5-Coder-3B-Instruct, zero-shot, no fine-tuning. Evaluated on Spider-dev and BIRD-dev. This is the true controlled baseline, since it is the same model, same quantization, same everything except training.

SFT arm: the same model, fine-tuned with LoRA on the Spider-train subset, using standard (question, schema, gold SQL) supervised pairs. Evaluated on the same two sets.

RL arm: initialized from the SFT checkpoint (matching the finding in Arctic-Text2SQL-R1 that RL starting from a stronger SFT checkpoint yields better downstream results than RL from an untrained base), then trained with GRPO using an execution-based reward. The primary reward is execution-only (does the generated SQL run and return the correct result), following Arctic-Text2SQL-R1's finding that a simple reward outperforms more complex partial-reward schemes. If time allows, a secondary run with a partial reward (adding syntax-validity and schema-linking terms, following Reasoning-SQL and SQL-R1) is trained as a stretch-goal ablation.

### Pre-RL SFT sanity check

Because the RL arm is initialized from the SFT checkpoint, RL can only refine behavior the checkpoint already exhibits; a checkpoint that has collapsed onto a few canned query templates, hallucinates schema elements, or produces truncated or empty output gets amplified rather than corrected once RL optimizes against it. `scripts/check_sft_checkpoint.py` runs once, after the SFT arm's `generate_sql.py` and `eval_sql.py` outputs exist and before the RL arm starts, and checks five things execution accuracy alone does not surface: execution accuracy and predicted-query execution-error rate, the rate of degenerate outputs such as an empty or placeholder query, the output length distribution, the diversity of generated query templates across different questions, and the rate of hallucinated schema references, i.e. table or column names in the predicted SQL that do not exist in that example's database. The last check matters specifically because a hallucinated reference can happen to execute without erroring, which a bare execution-accuracy number cannot distinguish from a correct query for the wrong reason.

The hallucination check resolves table aliases before comparing against the schema (`"students" AS "s"` followed by `"s"."name"` is checked as `students.name`, not as two unrelated tokens), so it can catch a real column attached to the wrong table, not just a nonexistent one. It is a regex-based heuristic, not a full SQL parser, so its own false-positive rate is measured directly rather than assumed to be zero: `scripts/calibrate_hallucination_floor.py` runs the same check against Spider-dev's gold SQL, which is correct by construction, and whatever nonzero rate comes back is the checker's own noise, not a real hallucination. That floor is currently 0.0000 on Spider-dev, after an earlier version of the check was found, via this same calibration step, to be misreading Spider gold's double-quoted string literals (e.g. `WHERE "Airline" = "JetBlue Airways"`, a SQLite convention) as hallucinated identifiers. `check_sft_checkpoint.py`'s hallucination threshold is set from this measured floor plus a small margin rather than picked arbitrarily. The script exits non-zero if any of its four gated checks (execution accuracy, degenerate-output rate, template-diversity ratio, hallucination rate) fails, so it can block the RL arm from starting on a checkpoint that has not passed this check. Note from the first real run against the SFT checkpoint: this model writes bare, unquoted identifiers (`t1.name`, following Spider's own `T1`/`T2` alias convention) rather than the double-quoted style the hallucination check looks for, so that specific gate passed vacuously (nothing to check) on this checkpoint -- the other three gates (execution accuracy, degenerate rate, diversity) passed meaningfully. The planned error-taxonomy pass, which compares predicted SQL structure against gold directly rather than relying on quoting conventions, is the more reliable source of schema/column-linking error rates for checkpoints like this one.

### RL post-training implementation

GRPO via TRL's `GRPOTrainer` (`scripts/train_rl.py`, `configs/rl.yaml`), continuing training on the SFT arm's LoRA adapter rather than a fresh one, matching the RL arm description above. GRPO was chosen over PPO because it needs no separate value model or reward model -- the advantage is computed from the mean and standard deviation of multiple sampled completions per prompt, which is the right fit for RLVR-style execution-verifiable rewards and is substantially lighter on an 8GB card than PPO's three-model setup would be. DPO was considered and rejected for this arm: it trains on offline preference pairs rather than online rollouts against a verifiable reward, which is a different experimental design than the online GRPO setup in SQL-R1, Reasoning-SQL, and Arctic-Text2SQL-R1 that this project scales down from.

vLLM/Unsloth are not used on the first attempt, despite being documented to cut GRPO's VRAM usage substantially, for two reasons specific to this project. First, the SFT arm already hit the 8GB ceiling during plain forward/backward at `max_seq_length=1024` with no rollout-generation memory added at all (`configs/sft.yaml`'s comment on that value); adding vLLM's second inference-mode memory region on top is a bigger first step than that history supports. Second, the SFT adapter was trained with plain `transformers` + `peft`, not Unsloth, and loading that adapter into an Unsloth-wrapped base model has not been verified to work. `configs/rl.yaml` sets `use_vllm: false`, so TRL falls back to plain `.generate()` for rollouts, reusing the same model/adapter-loading path `generate_sql.py` already proves works against this exact adapter. `num_generations` starts at 2, GRPO's mathematical minimum (the advantage `(r - mean) / std` is undefined for a single sample), rather than the 4 discussed as an open item in plan.md, until a short trial run confirms headroom via `nvidia-smi`/`free -h`. The KL penalty against the reference policy (`beta: 0.04`) does not require a second loaded model: TRL computes the reference log-probs by disabling the LoRA adapter on the same base model, a standard PEFT efficiency in TRL. The reward function (`scripts/rl_reward.py`) is built directly on `eval_sql.py`'s own `execute_query`/`rows_match`, not a reimplementation, so reward during training and execution accuracy during evaluation are guaranteed to mean the same thing.

This implementation was written and reviewed without GPU access, then verified against short real trial runs (`configs/rl_trial.yaml`) once training-hardware access was available. Two version-specific issues surfaced and were fixed rather than guessed at: `trl` 1.9.2's `GRPOConfig` does not accept `max_prompt_length` (confirmed against the installed `grpo_config.py` source; not passed, and not a practical problem since every `train_jsonl` row already fits `build_sft_data.py`'s `max_seq_length=1024` filter), and `GRPOConfig.temperature` defaults to `1.0`.

That second point led to a real finding from the trial runs, not a hypothetical: at the default temperature, `frac_reward_zero_std` -- the fraction of sampled GRPO groups where every completion got the identical reward, so the group contributes zero gradient -- was `1.0` at every single logged step across a full 10-step trial. Because `data.train_jsonl` is the exact rows the SFT arm already trained on for 3 epochs, the checkpoint had converged closely enough on many of these specific prompts that even temperature-1.0 resampling kept landing on the same completion every time, giving zero within-group reward variance regardless of `train_reward_mean`. Raising `temperature` to 1.3 (`configs/rl.yaml`) measurably reduced this in a follow-up trial (`frac_reward_zero_std` mixed between 1.0 and 0.5 rather than pinned at 1.0), though not eliminating it -- this is watched on every real run via the early-stop mechanism below, not assumed fixed from one trial.

### Ongoing RL health monitoring

The pre-RL sanity check above gates whether RL should *start*; it does not cover whether RL *stays* healthy while it runs, since reward hacking, template collapse, and schema hallucination can all be introduced by RL itself, not just inherited from the SFT checkpoint. `scripts/rl_health_callback.py` is a training callback that runs every `monitor.eval_steps` optimizer steps and checks: the trainer's own logged mean training-batch reward, reward standard deviation, `frac_reward_zero_std`, and KL-from-reference (all read from TRL's training log, not recomputed); and, using a held-out slice of Spider-dev built by `scripts/build_rl_heldout.py` (`data/sft/rl_heldout.jsonl`, disjoint from `train_jsonl` by construction of the Spider split), held-out execution accuracy computed with the identical reward function used in training, plus degenerate-output rate, template diversity, and schema-hallucination rate on that same held-out generation pass, reusing `check_sft_checkpoint.py`'s functions directly rather than reimplementing them.

Everything above is logging-and-warning only, deliberately -- reward-vs-accuracy divergence, degenerate rate, diversity, and hallucination are judgment calls a person should make in context, not thresholds safe to auto-abort a run over. One exception: if `frac_reward_zero_std` is at or above `0.9` on all of the first 3 health checks (`monitor.early_stop_after_checks`/`early_stop_frac_zero_std_threshold`), training stops itself rather than continuing to spend GPU-hours with confirmed-zero reward variance to learn from -- directly motivated by the trial-run finding above, where exactly this pattern occurred before the temperature fix. This check is deliberately scoped to only the first few checks and never re-armed afterward: later in a successful run, `frac_reward_zero_std` rising because the policy has converged to getting most groups right is a sign of success, not of the run being stuck, and should not trigger the same stop condition.

The concrete motivation for checking degenerate rate separately from the reward curve, rather than trusting reward alone, is not hypothetical for this project: verified directly against the training data, the question "count the states which have elevations lower than what alabama has" (`geo` database) has gold answer `0`, and the placeholder `SELECT 1=0` returns the same single row `(0,)` as the correct `COUNT(...)` query, so it scores full reward by coincidence. A policy that drifted toward emitting `SELECT 1=0` under uncertainty would look like it was improving on the reward curve alone; the degenerate-output-rate check on actual rollouts is what would catch it. The callback logs every check to `runs/rl_qwen2.5coder3b/health_log.jsonl` and prints warnings on threshold breaches; it does not stop training automatically, since auto-aborting a multi-hour run on a heuristic threshold is a larger and riskier decision than the one-shot pre-RL gate, and is left to whoever is watching the run.

### Metrics

Execution accuracy (EX) on Spider-dev and BIRD-dev, for each of the three arms. The primary generalization metric is the accuracy drop from Spider-dev to BIRD-dev, compared across arms: a smaller drop indicates better generalization from that post-training method.

### Error taxonomy

For each arm, incorrect predictions are categorized by comparing the generated SQL's structure and execution result against the gold query, using categories: schema/column linking errors, join errors, nested subquery errors, aggregation/GROUP BY errors, and syntax errors. The frequency of each category is reported per arm, to identify which failure modes SFT reduces and which RL reduces, rather than relying on aggregate accuracy alone. This analysis is the project's main original contribution, since the surveyed papers report aggregate execution accuracy but not this breakdown.

### Limitations to state explicitly in the final report

Training data volume is small relative to the papers this project scales down from, so absolute accuracy numbers are not expected to match published results and should not be compared to them directly. The GPTQ-Int8 7B reference is confounded, as noted above, and must be reported separately from the controlled three-arm result. If the fallback to 1.5B is needed, that substitution should be reported as a deviation from the original plan with the reason stated. No swap is configured on the training machine, so a training run that exhausts system RAM will crash rather than slow down; this should be treated as an operational risk, not a modeling one.

With only one out-of-distribution set (BIRD-dev), the generalization measure rests on a single train-to-test schema gap rather than an average or comparison across multiple OOD sets. This should be stated explicitly when reporting the generalization result, since a single OOD set is a weaker basis for a generalization claim than two would have been.

## Open items

Download Spider-train (official release or `xlangai/spider` on Hugging Face) before the SFT arm can start. Finalize the Spider-train subset size after timing the first SFT run. Decide whether the partial-reward RL ablation is in scope given remaining compute budget. Confirm whether GRPO rollout count (candidates per prompt) of 4 fits within 8GB VRAM alongside the 3B QLoRA setup, or whether it must be reduced to 2.

import json
import os

from check_sft_checkpoint import (
    check_schema_hallucination,
    is_degenerate,
    template_diversity,
)


def _latest_log_value(log_history, key):
    for entry in reversed(log_history):
        if key in entry:
            return entry[key]
    return None


def load_heldout_examples(heldout_jsonl, size):
    if not heldout_jsonl or not os.path.exists(heldout_jsonl):
        return []
    examples = []
    with open(heldout_jsonl, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples[:size]


try:
    from transformers import TrainerCallback as _TrainerCallback
except ImportError:
    # This sandbox has no transformers installed (see train_rl.py's NOTE);
    # fall back to a plain base so the pure-Python logic below stays
    # testable without the full training stack. On the machine that
    # actually runs training, transformers is installed, so RLHealthCallback
    # subclasses the real transformers.TrainerCallback there -- correct
    # integration with the Trainer's callback-handling, not a duck-typed
    # imitation of it.
    class _TrainerCallback:
        pass


class RLHealthCallback(_TrainerCallback):

    def __init__(self, tokenizer, tables_by_db, reward_fn, monitor_cfg, prompt_builder):
        self.tokenizer = tokenizer
        self.tables_by_db = tables_by_db
        self.reward_fn = reward_fn
        self.eval_steps = monitor_cfg["eval_steps"]
        self.max_degenerate_rate = monitor_cfg.get("max_degenerate_rate", 0.15)
        self.min_distinct_ratio = monitor_cfg.get("min_distinct_ratio", 0.20)
        self.max_hallucination_rate = monitor_cfg.get("max_hallucination_rate", 0.05)
        # If most sampled groups have zero reward variance, GRPO's advantage is 0 for
        # nearly everything and the run is effectively not learning regardless of
        # train_reward_mean -- see the frac_reward_zero_std comment in on_step_end.
        self.max_frac_reward_zero_std = monitor_cfg.get("max_frac_reward_zero_std", 0.80)
        self.log_path = monitor_cfg.get("log_jsonl")
        self.prompt_builder = prompt_builder  # (question, schema) -> chat messages

        # Early-run auto-stop -- see module docstring for why this is scoped to only
        # the first N checks and not re-armed later. Off by one flag
        # (_early_stop_evaluated) makes this a one-time decision, not a repeating gate.
        self.early_stop_enabled = monitor_cfg.get("early_stop_enabled", True)
        self.early_stop_after_checks = monitor_cfg.get("early_stop_after_checks", 3)
        self.early_stop_frac_zero_std_threshold = monitor_cfg.get(
            "early_stop_frac_zero_std_threshold", 0.9
        )
        self._frac_zero_std_history = []
        self._early_stop_evaluated = False

        self.heldout = load_heldout_examples(
            monitor_cfg.get("heldout_jsonl"), monitor_cfg.get("heldout_size", 50)
        )
        if not self.heldout:
            print(
                "[rl_health] WARNING: monitor.heldout_jsonl is not set (or the file "
                "doesn't exist), so held-out execution accuracy, degenerate-rate, "
                "diversity, and hallucination checks are DISABLED for this run -- only "
                "train_reward_mean and kl (from the trainer's own logs) will be tracked. "
                "See plan.md's open items."
            )

        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)

    def on_step_end(self, args, state, control, **kwargs):
        if state.global_step == 0 or state.global_step % self.eval_steps != 0:
            return control

        record = {
            "step": state.global_step,
            "train_reward_mean": _latest_log_value(state.log_history, "reward"),
            "reward_std": _latest_log_value(state.log_history, "reward_std"),
            # frac_reward_zero_std: fraction of sampled groups (num_generations
            # completions per prompt) whose reward std is 0, i.e. every completion
            # in the group got the same reward -- GRPO's advantage (r - mean) / std
            # is then 0 for that group, so it contributes no gradient. High values
            # here mean the run is mostly not learning regardless of what
            # train_reward_mean shows, which matters specifically because
            # data.train_jsonl is the same rows the SFT arm already trained on for
            # 3 epochs (configs/sft.yaml), so a checkpoint that already answers
            # most of them correctly can produce all-correct (zero-variance)
            # groups often. See plan.md's "Ongoing RL health monitoring" section.
            "frac_reward_zero_std": _latest_log_value(state.log_history, "frac_reward_zero_std"),
            "kl": _latest_log_value(state.log_history, "kl"),
        }

        if self.heldout:
            model = kwargs.get("model")
            tokenizer = self.tokenizer or kwargs.get("processing_class") or kwargs.get("tokenizer")
            if model is None or tokenizer is None:
                print(
                    "[rl_health] WARNING: could not find 'model'/tokenizer in the "
                    "callback kwargs this TRL version passes -- skipping the held-out "
                    "generation checks this step. train_reward_mean/kl above are still "
                    "logged from the trainer's own history."
                )
            else:
                record.update(self._run_heldout_checks(model, tokenizer))

        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")

        self._print_and_warn(record)
        self._check_early_stop(record, control)
        return control

    def _check_early_stop(self, record, control):
        if not self.early_stop_enabled or self._early_stop_evaluated:
            return

        frac = record.get("frac_reward_zero_std")
        if frac is not None:
            self._frac_zero_std_history.append(frac)

        if len(self._frac_zero_std_history) < self.early_stop_after_checks:
            return

        # Reached the check count -- decide once, then never re-evaluate (see
        # module docstring: this is deliberately an early-only gate).
        self._early_stop_evaluated = True
        recent = self._frac_zero_std_history[: self.early_stop_after_checks]
        if all(v >= self.early_stop_frac_zero_std_threshold for v in recent):
            print(
                f"[rl_health] STOPPING TRAINING at step={record['step']}: "
                f"frac_reward_zero_std was >= {self.early_stop_frac_zero_std_threshold} on "
                f"all of the first {self.early_stop_after_checks} health checks {recent} -- "
                "GRPO has had essentially no reward variance to learn from this early in the "
                "run, so the remaining steps are unlikely to fix that on their own. Consider "
                "raising num_generations, raising temperature further, or training on data "
                "the SFT checkpoint hasn't already memorized (see plan.md's 'Ongoing RL "
                "health monitoring' section). The last saved adapter checkpoint under "
                "output.run_dir is unaffected by this stop."
            )
            control.should_training_stop = True
        else:
            print(
                f"[rl_health] Early reward-variance check passed at step={record['step']}: "
                f"frac_reward_zero_std over the first {self.early_stop_after_checks} checks "
                f"was {recent} -- not uniformly at/above "
                f"{self.early_stop_frac_zero_std_threshold}, continuing training."
            )

    def _run_heldout_checks(self, model, tokenizer):
        import torch  # deferred: only needed once a real model/tokenizer exist

        was_training = model.training
        model.eval()
        completions = []
        try:
            with torch.no_grad():
                for ex in self.heldout:
                    messages = self.prompt_builder(ex["question"], ex["schema"])
                    prompt_text = tokenizer.apply_chat_template(
                        messages, tokenize=False, add_generation_prompt=True
                    )
                    inputs = tokenizer(prompt_text, return_tensors="pt", truncation=True, max_length=4096)
                    inputs = {k: v.to(model.device) for k, v in inputs.items()}
                    output_ids = model.generate(
                        **inputs, max_new_tokens=256, do_sample=False, num_beams=1,
                        pad_token_id=tokenizer.pad_token_id,
                    )
                    gen_only = output_ids[:, inputs["input_ids"].shape[1]:]
                    completions.append(tokenizer.batch_decode(gen_only, skip_special_tokens=True)[0])
        finally:
            if was_training:
                model.train()

        from sql_extract import extract_sql
        pred_records = [{"db_id": ex["db_id"], "pred_sql": extract_sql(c)}
                         for ex, c in zip(self.heldout, completions)]

        db_ids = [ex["db_id"] for ex in self.heldout]
        golds = [ex["gold_sql"] for ex in self.heldout]
        rewards = self.reward_fn([None] * len(self.heldout), completions, db_ids, golds)
        heldout_accuracy = sum(rewards) / len(rewards) if rewards else 0.0

        n_degenerate = sum(1 for r in pred_records if is_degenerate(r["pred_sql"]))
        degenerate_rate = n_degenerate / len(pred_records) if pred_records else 0.0
        diversity = template_diversity(pred_records)
        hallucination = check_schema_hallucination(pred_records, self.tables_by_db)

        return {
            "heldout_execution_accuracy": heldout_accuracy,
            "degenerate_output_rate": degenerate_rate,
            "diversity_distinct_ratio": diversity["distinct_ratio"],
            "schema_hallucination_rate": hallucination["rate"],
        }

    def _print_and_warn(self, record):
        step = record["step"]
        parts = [f"step={step}"]
        if record.get("train_reward_mean") is not None:
            parts.append(f"train_reward_mean={record['train_reward_mean']:.4f}")
        if record.get("reward_std") is not None:
            parts.append(f"reward_std={record['reward_std']:.4f}")
        if record.get("frac_reward_zero_std") is not None:
            parts.append(f"frac_reward_zero_std={record['frac_reward_zero_std']:.4f}")
        if record.get("kl") is not None:
            parts.append(f"kl={record['kl']:.4f}")
        if "heldout_execution_accuracy" in record:
            parts.append(f"heldout_ex_acc={record['heldout_execution_accuracy']:.4f}")
            parts.append(f"degenerate_rate={record['degenerate_output_rate']:.4f}")
            parts.append(f"distinct_ratio={record['diversity_distinct_ratio']:.4f}")
            parts.append(f"hallucination_rate={record['schema_hallucination_rate']:.4f}")
        print("[rl_health] " + " ".join(parts))

        if (record.get("train_reward_mean") is not None
                and "heldout_execution_accuracy" in record
                and record["train_reward_mean"] - record["heldout_execution_accuracy"] > 0.15):
            # NOTE: if reward.partial_credit_executes_wrong_result is set (see
            # configs/rl.yaml, rl_reward.py), train_reward_mean includes it and
            # heldout_execution_accuracy never does (train_rl.py builds the
            # health callback's reward_fn with partial_credit=0.0 on purpose,
            # so this number stays comparable to eval_sql.py). That means a
            # small structural gap up to roughly the partial_credit value is
            # expected even with zero overoptimization -- this warning's 0.15
            # threshold already has margin above the default 0.1 partial
            # credit, but don't read a ~0.1 gap alone as reward hacking.
            print(
                f"[rl_health] WARNING step={step}: train_reward_mean "
                f"({record['train_reward_mean']:.4f}) is notably higher than held-out "
                f"execution accuracy ({record['heldout_execution_accuracy']:.4f}) -- "
                "possible reward overoptimization, inspect recent rollouts."
            )
        if "degenerate_output_rate" in record and record["degenerate_output_rate"] > self.max_degenerate_rate:
            print(f"[rl_health] WARNING step={step}: degenerate_output_rate "
                  f"{record['degenerate_output_rate']:.4f} exceeds {self.max_degenerate_rate}")
        if "diversity_distinct_ratio" in record and record["diversity_distinct_ratio"] < self.min_distinct_ratio:
            print(f"[rl_health] WARNING step={step}: diversity_distinct_ratio "
                  f"{record['diversity_distinct_ratio']:.4f} below {self.min_distinct_ratio}")
        if "schema_hallucination_rate" in record and record["schema_hallucination_rate"] > self.max_hallucination_rate:
            print(f"[rl_health] WARNING step={step}: schema_hallucination_rate "
                  f"{record['schema_hallucination_rate']:.4f} exceeds {self.max_hallucination_rate}")
        if (record.get("frac_reward_zero_std") is not None
                and record["frac_reward_zero_std"] > self.max_frac_reward_zero_std):
            print(
                f"[rl_health] WARNING step={step}: frac_reward_zero_std "
                f"{record['frac_reward_zero_std']:.4f} exceeds {self.max_frac_reward_zero_std} -- "
                "most sampled groups have no reward variance, so GRPO's advantage is 0 for "
                "nearly everything; the run is likely producing little to no gradient signal "
                "regardless of what train_reward_mean shows."
            )

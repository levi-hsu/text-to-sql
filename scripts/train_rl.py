"""GRPO RL post-training for the RL arm (see plan.md, "models" -> "RL arm",
"Pre-RL SFT sanity check", and the RL-algorithm-choice discussion this
implements). Continues training the SFT arm's LoRA adapter with TRL's
GRPOTrainer, using the execution-based reward in rl_reward.py, monitored by
the ongoing-detection callback in rl_health_callback.py.

NOTE ON VERIFICATION: this script was written and reviewed, but NOT run --
this sandbox has no GPU, no CUDA, and no torch/trl/peft installed, so
nothing past pure-Python string/sqlite logic (rl_reward.py, sql_extract.py,
check_sft_checkpoint.py) could actually be executed here. Treat this as a
reviewed draft, not a tested one, and expect to debug the first real run,
most likely around GRPOTrainer's exact constructor kwargs (in particular
tokenizer= vs processing_class=, which TRL has renamed across versions --
check your installed trl.__version__'s GRPOTrainer signature if this
errors on that argument) and log_history's exact metric key names (this
assumes "reward" and "kl"; print state.log_history once after a few steps
and adjust rl_health_callback.py's _latest_log_value calls if the installed
version logs them under different keys, e.g. "rewards/mean").

Before running this: run scripts/check_sft_checkpoint.py against the SFT
arm's eval outputs and confirm it passes (see plan.md's "Pre-RL SFT sanity
check") -- RL can only refine behavior already present in the checkpoint it
starts from, so a checkpoint that fails that check should be fixed before
spending compute against it here, not after.

Usage:
  python scripts/train_rl.py --config configs/rl.yaml
"""

import argparse
import os

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel
from trl import GRPOConfig, GRPOTrainer

from data_utils import load_config
from prompt_template import build_messages
from rl_health_callback import RLHealthCallback
from rl_reward import make_execution_reward
from schema_utils import load_tables


def load_base_model_for_rl(model_cfg: dict):
    """Same 4-bit NF4 loading as train_sft.py's load_base_model_for_training
    -- the RL arm must load the base model identically to how the SFT arm
    did, since it is loading the SAME adapter on top and a mismatched
    quantization setup between the two would mean the adapter is no longer
    being applied to the weights it was trained against (generate_sql.py's
    configs/sft_eval.yaml comment makes this same point for eval loading).
    """
    model_path = model_cfg["name_or_path"]
    dtype = getattr(torch, model_cfg.get("dtype", "bfloat16"))

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=dtype,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=dtype,
        device_map="auto",
        quantization_config=bnb_config,
        attn_implementation=model_cfg.get("attn_implementation", "sdpa"),
    )
    return model


def build_prompt_dataset(train_jsonl: str) -> Dataset:
    """Builds the GRPO training dataset from the SAME rows the SFT arm
    trained on (data.train_jsonl in configs/rl.yaml points at
    data/sft/spider_train_subset.jsonl by default), so SFT and RL are
    compared under the same data budget (plan.md's central question).

    Only the prompt (schema + question) goes in the "prompt" column --
    unlike train_sft.py's SFTDataset, gold_sql is NOT part of the model
    input here, since GRPO generates its own completions and scores them
    against gold_sql via rl_reward.py instead of teacher-forcing on it.
    db_id and gold_sql are kept as extra columns so TRL forwards them to
    the reward function as kwargs (see rl_reward.py's reward_fn signature).
    """
    import json

    rows = []
    with open(train_jsonl, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows.append(
                {
                    "prompt": build_messages(row["question"], row["schema"]),
                    "db_id": row["db_id"],
                    "gold_sql": row["gold_sql"],
                }
            )
    return Dataset.from_list(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/rl.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    model_cfg = config["model"]
    data_cfg = config["data"]
    grpo_cfg = config["grpo"]
    output_cfg = config["output"]
    monitor_cfg = config["monitor"]
    reward_cfg = config.get("reward", {})
    partial_credit = reward_cfg.get("partial_credit_executes_wrong_result", 0.0)
    partial_credit_mode = reward_cfg.get("partial_credit_mode", "flat")
    graduated_floor = reward_cfg.get("graduated_floor", 0.02)
    graduated_max = reward_cfg.get("graduated_max", 0.3)

    if not torch.cuda.is_available():
        print("[train_rl] WARNING: CUDA not available. QLoRA + GRPO requires a CUDA "
              "GPU; this will fail or be unusably slow on CPU.")

    print(f"[train_rl] Loading tokenizer from {model_cfg['name_or_path']} ...")
    tokenizer = AutoTokenizer.from_pretrained(model_cfg["name_or_path"])
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"  # generation-heavy (rollouts), same as generate_sql.py

    print("[train_rl] Loading base model in 4-bit NF4 ...")
    base_model = load_base_model_for_rl(model_cfg)

    print(f"[train_rl] Loading SFT adapter from {model_cfg['sft_adapter']} as the RL "
          "arm's starting policy (plan.md: RL arm is initialized from the SFT checkpoint) ...")
    model = PeftModel.from_pretrained(base_model, model_cfg["sft_adapter"], is_trainable=True)
    model.print_trainable_parameters()

    print(f"[train_rl] Building prompt dataset from {data_cfg['train_jsonl']} ...")
    train_dataset = build_prompt_dataset(data_cfg["train_jsonl"])
    print(f"[train_rl] {len(train_dataset)} training prompts")

    # reward_fn is what GRPOTrainer actually optimizes against, and includes
    # partial_credit if configured (see rl_reward.py's module docstring and
    # configs/rl.yaml's reward.partial_credit_executes_wrong_result). The
    # health callback's held-out "execution accuracy" number needs to stay a
    # true, eval_sql.py-comparable accuracy -- not inflated by partial
    # credit -- so it gets a second reward_fn built with partial_credit
    # forced to 0.0, not the training one. Otherwise heldout_execution_accuracy
    # would silently stop meaning "fraction exactly correct."
    if partial_credit_mode == "graduated":
        print(f"[train_rl] Using partial_credit_mode=graduated (floor={graduated_floor}, "
              f"max={graduated_max}) for training reward -- 1.0 exact match, row-overlap-scaled "
              "between floor and max if the query executes but is wrong, 0.0 if it errors. "
              "Health-callback's heldout_execution_accuracy uses partial_credit=0.0/mode=flat "
              "regardless, so it stays comparable to eval_sql.py's execution_accuracy.")
    elif partial_credit:
        print(f"[train_rl] Using partial_credit={partial_credit} (flat mode) for training reward "
              "(1.0 exact match, this value if the query executes but is wrong, 0.0 if it "
              "errors). Health-callback's heldout_execution_accuracy uses partial_credit=0.0 "
              "regardless, so it stays comparable to eval_sql.py's execution_accuracy -- expect "
              "train_reward_mean to run structurally a bit above heldout_execution_accuracy as "
              "a result of that difference alone, not necessarily reward overoptimization.")
    reward_fn = make_execution_reward(
        data_cfg["db_dir"], partial_credit=partial_credit, partial_credit_mode=partial_credit_mode,
        graduated_floor=graduated_floor, graduated_max=graduated_max,
    )
    pure_accuracy_fn = make_execution_reward(data_cfg["db_dir"], partial_credit=0.0)
    tables_by_db = load_tables(data_cfg["tables"])

    output_dir = output_cfg["run_dir"]
    os.makedirs(output_dir, exist_ok=True)

    # algo dispatch: 'grpo' (default, unchanged behavior -- every existing config
    # omits this key and is byte-identical to before this branch was added),
    # 'dr_grpo' (Liu et al. 2025), or 'rloo' (Ahmadian et al. 2024). See
    # configs/bird_adapt_rl_rloo.yaml / _drgrpo.yaml headers: these are meant to be
    # run with the SAME checkpoint, SAME data, and SAME grpo.* hyperparameters as
    # their GRPO counterpart (configs/bird_adapt_rl.yaml or
    # bird_adapt_rl_v2_phase{1,2}.yaml) -- algo is the only thing that should vary,
    # so it is kept as one extra top-level config key rather than duplicated
    # hyperparameters that could drift out of sync.
    algo = config.get("algo", "grpo").lower()
    if algo not in ("grpo", "dr_grpo", "rloo"):
        raise ValueError(f"Unknown algo '{algo}' in config -- expected 'grpo', 'dr_grpo', or 'rloo'.")
    print(f"[train_rl] algo={algo}")

    # Shared hyperparameters across all three algorithms -- deliberately identical
    # regardless of algo, so an algo swap is the only thing that differs between
    # otherwise-matched runs. Field-by-field the same as the GRPOConfig call this
    # replaced; see git history / bird_adapt_rl.yaml's comments for why each value
    # was chosen (temperature=1.3 to fight zero-reward-variance groups,
    # max_prompt_length intentionally not passed, etc.) -- none of that reasoning
    # changes with algo.
    shared_kwargs = dict(
        output_dir=output_dir,
        num_generations=grpo_cfg["num_generations"],
        per_device_train_batch_size=grpo_cfg["per_device_train_batch_size"],
        gradient_accumulation_steps=grpo_cfg["gradient_accumulation_steps"],
        learning_rate=grpo_cfg["learning_rate"],
        beta=grpo_cfg["beta"],
        temperature=grpo_cfg.get("temperature", 1.0),
        num_train_epochs=grpo_cfg.get("num_train_epochs", 1),
        max_steps=grpo_cfg.get("max_steps", -1),
        max_completion_length=data_cfg["max_completion_length"],
        use_vllm=grpo_cfg.get("use_vllm", False),
        logging_steps=grpo_cfg.get("logging_steps", 5),
        save_strategy=grpo_cfg.get("save_strategy", "steps"),
        save_steps=grpo_cfg.get("save_steps", 50),
        save_total_limit=grpo_cfg.get("save_total_limit", 3),
        bf16=grpo_cfg.get("bf16", True),
        gradient_checkpointing=grpo_cfg.get("gradient_checkpointing", True),
        optim=grpo_cfg.get("optim", "paged_adamw_8bit"),
        report_to=grpo_cfg.get("report_to", "none"),
        remove_unused_columns=False,  # keep db_id/gold_sql columns for the reward function
    )

    health_callback = RLHealthCallback(
        tokenizer=tokenizer,
        tables_by_db=tables_by_db,
        reward_fn=pure_accuracy_fn,  # partial_credit=0.0 -- see comment above
        monitor_cfg=monitor_cfg,
        prompt_builder=build_messages,
    )

    if algo in ("grpo", "dr_grpo"):
        extra_kwargs = {}
        if algo == "dr_grpo":
            # Dr. GRPO (Liu et al. 2025, "Understanding R1-Zero-Like Training"):
            # same GRPOTrainer, two of GRPO's biases removed via config flags rather
            # than a different trainer class. scale_rewards=False drops the
            # (r - mean) / std advantage down to plain (r - mean), so a group with a
            # small nonzero std (a rare success in an otherwise-all-wrong group,
            # common on this project's "hard"-bucketed BIRD examples) no longer gets
            # its advantage inflated by dividing by that small std. loss_type=
            # "dr_grpo" replaces GRPOTrainer's default per-sequence-length loss
            # normalization with a constant-length normalization, removing the
            # length bias the paper identifies. NOT verified against this project's
            # installed trl version -- both kwargs were added to GRPOConfig after
            # the trl>=0.12 floor in requirements.txt. If this raises TypeError,
            # your installed trl predates them; run
            #   python -c "import trl; print(trl.__version__)"
            #   python -c "from trl import GRPOConfig; import inspect; print(inspect.signature(GRPOConfig.__init__))"
            # to confirm, and upgrade trl if so.
            extra_kwargs = {"scale_rewards": False, "loss_type": "dr_grpo"}
        try:
            training_args = GRPOConfig(**shared_kwargs, **extra_kwargs)
        except TypeError as e:
            raise TypeError(
                f"[train_rl] GRPOConfig rejected algo='{algo}' kwargs {list(extra_kwargs)}: {e}. "
                "This almost always means the installed trl version predates scale_rewards/"
                "loss_type support on GRPOConfig -- check trl.__version__ and upgrade trl, "
                "or set algo: grpo in this config to fall back to the original behavior."
            ) from e
        trainer = GRPOTrainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
            reward_funcs=[reward_fn],
            processing_class=tokenizer,  # see top-of-file NOTE: some trl versions use tokenizer=
            callbacks=[health_callback],
        )
    else:  # algo == "rloo"
        # RLOO (Ahmadian et al. 2024, "Back to Basics: Revisiting REINFORCE-Style
        # Optimization for Learning from Human Feedback in LLMs"): leave-one-out
        # baseline, advantage = r_i - mean(r_{-i}), no std division. Uses trl's own
        # RLOOTrainer/RLOOConfig directly rather than approximating the formula
        # inside GRPOTrainer, so this gets TRL's actual RLOO implementation, not a
        # hand-rolled stand-in for it.
        #
        # UNVERIFIED against this project's installed trl version -- this branch has
        # never been exercised against a real RLOOTrainer (same caveat this file's
        # top-of-file NOTE originally flagged for GRPOTrainer before its first real
        # run). Two things to check if this errors on the first real run:
        #   1. RLOOConfig's field names may not match GRPOConfig's one-for-one --
        #      read the TypeError below, drop/rename the offending kwarg in
        #      shared_kwargs.
        #   2. RLHealthCallback (rl_health_callback.py) reads "reward",
        #      "reward_std", "frac_reward_zero_std", and "kl" from
        #      state.log_history, which is what GRPOTrainer logs them as. If
        #      RLOOTrainer logs these under different keys, the callback will just
        #      print fewer fields -- it checks each key for None before using it,
        #      so this fails safe (degrades to less logging), it does not crash
        #      training. Print state.log_history after a few steps and update
        #      rl_health_callback.py's _latest_log_value calls if you want those
        #      fields back.
        try:
            from trl import RLOOConfig, RLOOTrainer
        except ImportError as e:
            raise ImportError(
                "[train_rl] algo='rloo' requires trl to export RLOOConfig/RLOOTrainer -- "
                "your installed trl version does not. Run "
                "`python -c \"import trl; print(trl.__version__)\"` and upgrade trl "
                "(pip install -U trl) if it's missing."
            ) from e
        try:
            training_args = RLOOConfig(**shared_kwargs)
        except TypeError as e:
            raise TypeError(
                f"[train_rl] RLOOConfig rejected one of the GRPOConfig-style kwargs in "
                f"shared_kwargs: {e}. RLOOConfig's field names may not match GRPOConfig's "
                "one-for-one in your installed trl version -- check "
                "`python -c \"from trl import RLOOConfig; import inspect; print(inspect.signature(RLOOConfig.__init__))\"` "
                "and adjust shared_kwargs in train_rl.py accordingly."
            ) from e
        trainer = RLOOTrainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
            reward_funcs=[reward_fn],
            processing_class=tokenizer,
            callbacks=[health_callback],
        )

    trainer.train()

    adapter_dir = os.path.join(output_dir, "adapter")
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    print(f"[train_rl] Saved RL-trained LoRA adapter + tokenizer to {adapter_dir}")


if __name__ == "__main__":
    main()

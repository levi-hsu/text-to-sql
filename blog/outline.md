<!--
Style model: https://transformer-circuits.pub/2026/nla/index.html
("Natural Language Autoencoders Produce Unsupervised Explanations of LLM
Activations," Anthropic, May 2026).

That piece is a full research-paper structure, not the lighter
consumer-blog structure of posts like "Tracing the thoughts of a large
language model": it has an abstract, a related-work section, a method
section with real technical detail (architecture, training step, reward
shaping), a quantitative-results section BEFORE the narrative case
studies, named case studies that each isolate one mechanism, an explicit
discussion-and-limitations section, and a citation/author-contributions
block at the end. The section order below mirrors that shape.

Every bullet below either states a real number already computed in
runs/blog_artifacts/, or is marked [DRAFT] / [DECIDE] where it needs your
input. Nothing here is placeholder filler dressed as content.
-->

# Title

[DECIDE -- pick one, or draft your own]
- "SFT vs. RL for Text-to-SQL: A Controlled Comparison at 3B Scale"
- "Post-training Improves Text-to-SQL Accuracy and Widens Its Generalization Gap"
- "What RL Post-training Actually Fixes in Text-to-SQL, and What It Doesn't"

The second option states the headline finding directly in the title,
which is closer to this reference's own title style (its title is
literally its main claim, not a teaser).

# Authors and metadata

- Byline, date, link to the repo (scripts/, configs/, plan.md).
- [DECIDE] Whether to include a short "Published [date]" line the way
  the reference does immediately under the title.

# Abstract

One dense paragraph, written last, after every number below is final.
Must state, in this order, matching the reference's abstract shape
(what we built -> what we optimized for -> what we found -> the
surprising/notable part -> what's released):

1. The central question, from plan.md's Goal section: given the same
   base model and training budget, how much does SFT vs. RL post-training
   improve execution accuracy over a zero-shot baseline, and which
   generalizes better from Spider (in-distribution) to BIRD
   (out-of-distribution).
2. The setup in one sentence: Qwen2.5-Coder-3B-Instruct, QLoRA, three
   controlled arms (baseline / SFT / RL, plus an RL-v2 variant), scaled
   down from SQL-R1 / Reasoning-SQL / Arctic-Text2SQL-R1.
3. The headline quantitative result (real, from
   runs/blog_artifacts/experiment1_scorecard.txt):
   Spider-dev execution accuracy: baseline 0.6083, SFT 0.6973, RL 0.6973
   (identical to SFT), RL-v2 0.7128.
4. The counterintuitive part, which should anchor the abstract the way
   the reference's abstract leads with its most surprising finding
   (unverbalized evaluation awareness): every post-trained arm has a
   *larger* Spider-to-BIRD accuracy drop than the zero-shot baseline
   (baseline drop 0.4010; SFT 0.5095; RL 0.5017; RL-v2 0.5276). Post-training
   makes the model better in-distribution and relatively worse at
   generalizing out-of-distribution.
5. The original contribution: an error-taxonomy breakdown showing *which*
   failure modes each method fixes, not just aggregate accuracy -- the
   thing plan.md notes the surveyed papers don't report.
6. What's released: training/eval scripts, configs, taxonomy tooling.

# Introduction

- Open with why aggregate execution accuracy is an incomplete signal:
  two models can score the same EX% while failing in completely
  different ways, and a reader has no way to tell "fixed schema linking"
  from "fixed nothing, got lucky on different examples" from a single
  number.
- State the central question (same as plan.md's Goal section, restated
  in prose, not copied verbatim).
- Forecast the two main findings from the Abstract in plain language,
  the way the reference's introduction previews "surfaced unverbalized
  evaluation awareness" before the reader reaches that section.
- [DRAFT] One or two sentences on why 3B/single-GPU scale is still worth
  reporting: SQL-R1 / Reasoning-SQL / Arctic-Text2SQL-R1 all show RL
  beating SFT at 7B-32B with large compute budgets; this asks whether the
  same *direction* of effect holds at a scale anyone with a consumer GPU
  can replicate.

# Related work

- SQL-R1, Reasoning-SQL, Arctic-Text2SQL-R1: what they show (RL
  post-training beating SFT-only by several EX points at 7B-32B), and
  that Qwen2.5-Coder is their shared base-model family, which is why this
  project's results are comparable in kind, if not magnitude.
- OmniSQL and any other Qwen2.5-Coder-based text-to-SQL work worth a
  sentence each. [DRAFT -- confirm which papers you actually want cited;
  plan.md names the first three explicitly.]
- The gap this project fills, stated plainly: none of the above report an
  error-category breakdown -- they report aggregate EX and stop.

# Method

Mirrors the reference's Method section in that it should include real
implementation detail, not just "we fine-tuned the model." Pull directly
from plan.md, which already has this written at the right level of
detail -- this section is largely assembly, not new writing.

### Model and quantization
- Qwen2.5-Coder-3B-Instruct, QLoRA (4-bit NF4 frozen base + bf16 LoRA
  adapters). Qwen2.5-Coder-7B-Instruct-GPTQ-Int8 as an explicitly
  confounded reference point, reported separately (plan.md lines 21, 87).

### Datasets
- Spider train/dev as the in-distribution set; BIRD-dev as the untouched
  out-of-distribution set; BIRD-train excluded (too large for this
  hardware) and Spider-DK dropped from scope (plan.md lines 27-33).

### Experimental arms
- Baseline (zero-shot), SFT (LoRA on filtered Spider-train subset), RL
  (GRPO from the SFT checkpoint, execution-only reward), RL-v2 (same but
  starting from an earlier SFT checkpoint-500, num_generations 2->4, plus
  a partial-credit reward term -- plan.md lines 47-51 and
  compare_arms.py's header comment for the exact rationale).

### RL implementation
- GRPO via TRL's GRPOTrainer, why GRPO over PPO/DPO (plan.md lines 61),
  why vLLM/Unsloth were not used on the first attempt (plan.md lines 63),
  the real temperature finding from the trial runs: at temperature 1.0,
  frac_reward_zero_std was 1.0 at every logged step because the SFT
  checkpoint had already converged on these exact prompts; raising to 1.3
  measurably reduced it (plan.md lines 65-67). This is a good candidate
  for a short "what we tried that didn't work at first" aside, matching
  the reference's own documented dead ends (e.g. "we had set out to show
  the model didn't plan ahead, and found instead that it did").

### Pre-RL sanity check and ongoing RL health monitoring
- check_sft_checkpoint.py's four gates, the calibrated hallucination
  floor (0.0000 on Spider-dev gold), and the concrete degenerate-output
  motivation: `SELECT 1=0` scores full reward by coincidence on the geo
  "elevations lower than Alabama" question because the gold answer is
  also 0 rows (plan.md lines 55-57, 75). This is a strong, concrete
  paragraph -- keep it close to plan.md's own wording.

# Applying and evaluating the three arms

Mirrors the reference's own top-level section name and internal order:
quantitative results first, then named case studies, then a secondary
ablation.

## Quantitative results

- Full scorecard table: source runs/blog_artifacts/experiment1_scorecard.txt.
  Both Spider-dev and BIRD-dev, all four arms, plus the generalization
  drop row.
- Error-taxonomy table: source runs/blog_artifacts/experiment1_error_taxonomy.txt.
  Lead with the real pattern already visible in that file: baseline's
  schema_column_error rate (0.3325) and join_structure_mismatch rate
  (0.2705) on Spider-dev are both higher than every post-trained arm's
  (schema_column_error drops to 0.24-0.25; join_structure_mismatch drops
  to 0.21-0.22) -- SFT and RL measurably reduce structural/schema-linking
  errors. At the same time, other_wrong_result rises from 0.2705
  (baseline) to 0.35-0.37 (post-trained arms) -- post-training is
  trading structural errors for value-level ones (wrong constant, wrong
  column choice among valid columns), the category the regex-based
  taxonomy can't subdivide further. State this as the second major
  finding, on equal footing with the generalization-drop finding in the
  abstract.

## Case studies

Three case studies, same function as "Planning in Poetry" /
"Reasoning about Rewards" / "Evaluation vs. deployment" in the reference:
each names one mechanism and shows it happening in real output, pulled
directly from runs/blog_artifacts/*_gallery.md -- these are real dev-set
examples with real executed results, not constructed illustrations.

1. **A clean structural fix** -- pick one example where baseline gets a
   join or schema-linking question wrong and SFT/RL get the identical
   question right, executed side by side. Source:
   baseline_spider_dev_gallery.md vs. sft_spider_dev_gallery.md, same
   `db_id`/question if one exists in both, or the nearest matching pair.
   [DRAFT -- needs a manual pass to find a same-question pair across
   galleries; error_taxonomy.py's stored examples aren't cross-indexed by
   question across arms.]

2. **What "the model messes up joins" looks like in practice** -- the
   already-surfaced SFT syntax-error example on `dog_kennels`: predicted
   SQL contains `FROM treatments AS t AS JOIN dogs AS t3 ... AS JOIN
   professionals AS t1`, a literal double-`AS` malformation, vs. gold's
   clean `JOIN ... JOIN`. Source: sft_spider_dev_gallery.md, "Syntax
   error" section. Good opening case study because it needs no execution
   accuracy background to understand -- the reader can just look at the
   SQL.

3. **The generalization drop, concretely** -- one example where the same
   arm (suggest RL-v2, the strongest in-distribution arm) is correct on
   a Spider-dev question and wrong on a structurally similar BIRD-dev
   question, to make the abstract's headline number
   legible as an actual failure rather than just a percentage. Source:
   rl_v2_spider_dev_gallery.md vs. rl_v2_bird_dev_gallery.md.
   [DRAFT -- pick the clearest pair.]

## Does the RL algorithm choice matter

Secondary ablation section, playing the same role as the reference's
"Training an NLA and SAE" comparison subsection: a direct, honest
null-result report.

- RLOO and Dr. GRPO were run as algorithm-swap replicates of the same
  RL-continue / RL-continue-v2 setup (same checkpoint, same data --
  scripts/run_rl_algo_variants_chain.sh). Real crossdb_transfer numbers,
  source runs/blog_artifacts/experiment2_scorecard.txt:
  rl-continue (GRPO) 0.2035, rl-continue-rloo 0.2054, rl-continue-drgrpo
  0.2035, rl-continue-v2 0.2035, rl-continue-v2-rloo 0.2007,
  rl-continue-v2-drgrpo 0.2026. State plainly: the algorithm swap moves
  the result by at most 0.002-0.005, well inside noise for a 1071-example
  eval set -- the choice of GRPO vs. RLOO vs. Dr. GRPO did not matter
  here, in contrast to what the RL-continue-v2 curriculum vs. RL-continue
  (v1) comparison shows (both land at the same 0.2035, too, actually --
  [DRAFT: state directly that in this experiment neither the algorithm
  nor the curriculum phasing beat plain RL-continue by a meaningful
  margin; only SFT-continue did, see next section]).

# A harder test: adapting to BIRD with a little real data

Second top-level experiment, same structural role as the reference's
"Automated auditing benchmark" section -- a harder, more realistic
follow-up test beyond the main case studies.

- Setup: starting from the Spider-only SFT checkpoint, continue training
  on a small 122-example pool of real BIRD-train-like data (two schemas),
  then evaluate on three slices: spider_retention (did it forget
  Spider), pool_heldout (same schemas, unseen questions -- memorization
  check), crossdb_transfer (9 databases disjoint from training -- the
  real transfer test). Source: plan.md's Experiment 2 design (implicit
  in scripts/run_experiment2_chain.sh, run_bird_adapt_eval.sh) and
  runs/blog_artifacts/experiment2_scorecard.txt.
- Headline result: sft-continue crossdb_transfer 0.2120, beating the
  restricted spider-only baselines on the same db_ids (baseline 0.2236
  restricted -- note sft-continue is still *below* the zero-shot
  baseline here, by -0.0116; it only beats the spider-only SFT/RL-v2
  arms, by +0.0181/+0.0171). State this precisely -- don't round it up to
  "continuation training helps," since it only helps relative to
  spider-only post-training, not relative to doing nothing.
- The striking, real finding worth its own short case study: rl-continue
  pool_heldout collapses to 0.0323 (1/31 correct) vs. sft-continue's
  0.2581 (8/31) on the exact same 31 held-out questions. RL continuation
  on this small a pool looks like catastrophic overfitting/collapse
  relative to SFT continuation, even though both start from the same
  checkpoint and see the same training pool. [DRAFT -- worth one
  paragraph of speculation on why: the pool is small (122 examples),
  which is exactly the regime the plan.md's own health-monitoring section
  worries about (frac_reward_zero_std, template collapse), so this is a
  natural place to check runs/bird_adapt_rl_eval/health_log.jsonl before
  writing the explanation, rather than guessing.]

# Discussion and limitations

Pull directly from plan.md's own "Limitations to state explicitly in the
final report" section (lines 85-89) -- it is already written at
publication quality and should not be rewritten from scratch, only
lightly adapted to first person:

- Training data volume is small relative to the papers this scales down
  from; absolute accuracy numbers should not be compared to published
  results directly.
- The GPTQ-Int8 7B reference point is confounded on three dimensions at
  once (parameter count, quantization, training status) and must stay
  reported separately from the controlled three-arm result.
- Single OOD set (BIRD-dev only): the generalization measure rests on
  one train-to-test schema gap, a weaker basis for a generalization claim
  than averaging across multiple OOD sets would be.
- No swap configured on the training machine: a run that exhausts system
  RAM crashes rather than degrades -- an operational risk, not a modeling
  one.
- The error taxonomy itself is heuristic, not a SQL parser (regex-based
  JOIN/subquery/aggregate counting) -- error_taxonomy.py's own docstring
  lists its known gaps (e.g. table_reference_mismatch misses
  subquery-derived FROMs by construction). State this the way the
  reference states NLA's confabulation limitation: directly, not
  buried in a footnote.

[DRAFT] One synthesis paragraph on what surprised you going in, matching
both reference posts' "we expected X, found Y" convention -- this has to
come from you, not from the run logs. Candidate honest material: did you
expect post-training to *help* generalization rather than hurt it, going
in? Did the RL-continue pool_heldout collapse surprise you, or match a
concern you already had from the health-monitoring design?

# Conclusion and what's next

- Restate the two headline findings in one sentence each.
- Open items, adapted from plan.md lines 91-93 and updated to reflect
  what's actually still open now that the runs exist: confirm whether
  the partial-reward RL ablation is worth running given the RL-continue
  pool_heldout result; whether a second OOD set is worth adding before
  trusting the generalization claim further.

# Reproducibility

- Link the repo. List the entry points a reader could actually run:
  scripts/run_baseline_eval.sh, run_sft.sh, run_rl_eval.sh,
  run_bird_eval.sh, error_taxonomy.py, compare_arms.py,
  compare_error_taxonomy.py, run_blog_artifacts.sh (the exact pipeline
  that produced every number and gallery in this post).

# Acknowledgements

[DECIDE -- optional for a solo project; the reference includes this
because it's a multi-author lab paper. Cut this section entirely if it
doesn't fit a personal blog, rather than leaving it empty.]

# Citation

[DECIDE -- optional. The reference includes a BibTeX-style block because
it's an academic-adjacent publication meant to be cited. A personal blog
post likely doesn't need this; cut it unless you want the post treated as
a citable artifact.]

# Text-to-SQL Task Specification

Priority order for this document: task definition, then datasets, then
algorithm/model choice -- the latter is intentionally deferred to PLAN.md,
which now also covers the RL-vs-SFT research question and training
infrastructure.

## What the task is

Given a natural-language question and a database, produce the SQL query
that answers it when the query is executed against that database. The
model is not being asked to know facts about the world -- it is being
asked to translate a stated intent into a formal query, using the
database's schema as the only source of truth. The combinatorial search
space is large and irregular (SQL has joins, aggregation, nesting,
subqueries), but the correctness check is still a program that runs
automatically: this is an execution-verifiable-reward task, meaning the
reward or label used to judge a candidate query requires no LLM judge
and no human annotation at training or evaluation time.

## Formal definition

Input: a pair `(question, schema)`, where `schema` lists the database's
tables, columns, column types, and primary/foreign key relationships --
not the data rows themselves.

Output: a single SQL query string.

Success criterion: the query executes without error against the actual
database and returns the same result set as a human-written gold query.
This is called execution accuracy (EX). It is not exact string match,
because many SQL strings are semantically equivalent -- different join
orders, different column orders, different but logically identical
`WHERE` clause forms. EX is computed by running both queries and
diffing their result tables, so it requires no LLM judge and no human
labeling at training time.

## What "general" text-to-SQL means here

You asked whether it's possible to focus on the general use of
text-to-SQL rather than a specific benchmark. The general version of
this task is not "handle arbitrary natural language in any SQL
dialect" -- it is schema-agnostic: given a database the model has never
seen during training, and only that database's schema at inference
time, produce a correct query. A model that only works on one
memorized schema has not learned text-to-SQL at all; it has memorized a
lookup table. This generalization property is exactly what the two
standard datasets below are built to test -- both enforce disjoint
databases between train and test splits, so scoring well on either
requires the schema-agnostic skill, not memorization.

## Datasets

### Spider

10,181 questions, 5,693 unique SQL queries, 200 databases spanning 138
domains, released by Yale in 2018. Train and dev splits use entirely
disjoint databases (8,659 training instances / 1,034 dev instances),
which is exactly the out-of-distribution generalization test described
above. Schemas are simpler (single-table up to two or three table
joins), and the databases themselves are synthetic/curated rather than
scraped from the real world. Good as an easier first training stage:
simpler schemas and queries mean faster iteration while the pipeline
itself is still being debugged.

### BIRD

12,751 question-SQL pairs over 95 real databases, 33.4 GB of total
database content, spanning 37 professional domains, released in 2023.
Substantially harder than Spider: databases contain dirty/messy real
values, many questions require external domain knowledge not stated in
the schema, and individual databases are much larger. Standard metric
is execution accuracy, with a secondary "valid efficiency score" (VES)
that penalizes needlessly slow queries -- not the primary target.

Calibration numbers for this benchmark, confidence noted per figure:

- GPT-4, prompting only, no fine-tuning: 54.89% EX (high confidence,
  reported directly in the BIRD paper).
- Best published prompting-only pipelines as of this writing (multi-
  candidate generation + selection over GPT-4/GPT-4o): roughly 63-65%
  EX (medium confidence, drawn from search-engine synthesis of several
  papers, not independently re-verified against each primary source).
- Human performance: 92.96% EX (high confidence, BIRD paper).
- SLM-SQL, a 1.5B-parameter model trained with SFT then RL post-
  training on the public SynSQL-2.5M-derived dataset: 67.08% EX on the
  BIRD dev set, 70.49% on the BIRD test set (medium-high confidence --
  confirmed consistently across the paper's own abstract, ACL
  Anthology listing, and independent summaries, but I have not read
  the full methodology section myself).

The SLM-SQL number is a useful precedent for the SFT-then-RL recipe
class in general, though the model-scale match is looser now that this
project's base model is Qwen2.5-Coder-7B-Instruct rather than a
1.5-2B-class model (SLM-SQL is 1.5B, roughly a fourth of this project's
size) -- directionally supportive, not a tight quantitative match.

### Recommended staging

SFT warm-start on Spider first (smaller schemas, simpler queries,
faster iteration while the pipeline itself is being debugged), then
SFT/RL on BIRD (the harder, more realistic target difficulty) if the
project is extended past Spider. Both datasets are public and free to
download for research use.

## Why this fits the constraints from the earlier request

The task needs to be useful for daily life or academic work, within
this model's capacity, not achievable by prompting alone, and
trainable with accessible data. Point by point:

- Not achievable by a 7B model through prompting alone at competitive
  accuracy: this is a claim about model scale, not about the task
  category in general -- frontier models solve a meaningful fraction
  of BIRD through prompting alone (GPT-4 above). Nothing found in this
  search establishes that a 7B-class model reaches competitive accuracy
  through prompting alone either; SLM-SQL's own premise is that small
  models "underperform on Text-to-SQL tasks due to limited logical
  reasoning capabilities" and required SFT plus RL specifically to
  close that gap, though SLM-SQL's own model is smaller (1.5B) than
  this project's base model, so the claim is weaker at 7B than it would
  be at 1.5B and should be checked empirically via the project's own
  zero-shot baseline rather than assumed.
- Achievable with training at this scale: SLM-SQL, Arctic-Text2SQL-R1,
  and CSC-SQL are all existing evidence of SFT-then-RL recipes closing
  the gap at 7B or smaller (see PLAN.md's "Why RL fits this task" for
  the fuller precedent list).
- Trainable with accessible data: both Spider and BIRD are public,
  free, and standard.
- Execution-verifiable reward: the verifier is a SQL-execution diff
  against the gold query's result table, which serves double duty as
  both the evaluation metric (EX) and, if the project's RL stage goes
  forward, the RL reward signal, with no LLM judge or human annotation
  needed for either.

Confidence that this is a good task overall: approximately 65%.
The main unchecked risk is feasibility, not desirability -- whether
Qwen2.5-Coder-7B-Instruct's usable context window and inference
speed on the project's training hardware comfortably handle BIRD's
larger schemas (some of its databases have dozens of tables, meaning
a linearized schema alone can run to several hundred tokens before
the question or any reasoning begins), if the project is extended to
BIRD. For the current Spider-only scope in PLAN.md this risk is lower,
since Spider's schemas are smaller. This has not been tested and
should be checked empirically before committing to a BIRD extension.
(Training hardware changed from a Mac M2 to a Windows/CUDA machine
partway through the project, see PLAN.md's Overall goal section --
doesn't change this risk assessment materially, since the concern is
context length and schema size, not platform.)

## Explicitly deferred to PLAN.md

Per task, then datasets, then algorithm and model choice: this document
stops at datasets on purpose. Decided in PLAN.md instead: exact schema
linearization format for the prompt, SFT label/chat-template design,
final confirmation of the base model, whether to include an RL
post-training stage, and if so, which RL algorithm -- GRPO, PPO,
RLOO/REINFORCE-style methods, and rejection-sampling fine-tuning (RAFT
/ expert iteration) are all mainstream, applicable candidates for an
execution-verifiable-reward task like this one; none is assumed by
this document.

## Sources

- [Bird: Database-Grounded Text-to-SQL Benchmark (arXiv:2305.03111)](https://arxiv.org/abs/2305.03111)
- [BIRD benchmark overview -- Stanford measurement-db](https://aimslab.stanford.edu/measurement-db/bird_sql)
- [Spider: Yale Semantic Parsing and Text-to-SQL Challenge](https://yale-lily.github.io/spider)
- [Spider Benchmark: Datasets and Evaluation -- Emergent Mind](https://www.emergentmind.com/topics/spider-benchmark)
- [SLM-SQL: An Exploration of Small Language Models for Text-to-SQL (arXiv:2507.22478)](https://arxiv.org/abs/2507.22478)
- [SLM-SQL -- ACL Anthology](https://aclanthology.org/2025.findings-ijcnlp.92/)

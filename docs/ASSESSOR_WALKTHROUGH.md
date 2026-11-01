# Showing PipelineProof: a 10-minute walkthrough

For presenting the product to an assessor, an investor or a pilot employer. Everything in the demo is
synthetic, and every number on screen comes from really running the code.

## Before you start

Open the site and check `/evidence` shows the latest benchmark run. The first page load after a quiet
period can take a few seconds while a container starts.

## The route

1. **Home page (1 minute).** The problem in one sentence: take-home results are read, not run. Point at
   the example report on the right: claimed 0.971, unseen customers 0.807.
2. **Assessor demo → Enter the demo workspace.** No account needed. Open the sample assessment: seven
   synthetic candidates, each with a report.
3. **Amelia Hart (clean pipeline).** Claim reproduced exactly; unseen customers score slightly lower,
   which is expected; no findings. The reviewer confirmed "advance" with a reason, and feedback was
   released.
4. **Rohan Mehta (leaky pipeline).** Same claim of about 0.97, but 0.807 on unseen customers. The
   critical finding comes from an ablation re-run: setting `retention_offer_sent` to its value on the
   1st of the month drops his own score to 0.831. Show the reviewer's decision, the feedback, and the
   candidate's contest with its answer. This is the human-in-the-loop story.
5. **Chloe Okafor (random split).** The report splits her own test set into customers seen and unseen
   in training, with a confidence interval. That is group leakage, measured.
6. **Live benchmark runs → `p13_xgboost_leak` → Run live.** The page shows each step as it happens:
   environment, four runs in sandboxes, trace analysis, ablation. It takes about a minute.
7. **Candidate's side.** From the assessment, *Open ↗* next to Rohan: the notice, the brief, the data,
   the upload with its pre-flight check, the released feedback and the contest form.
8. **Audit log.** Every decision, release and evaluation, with who and when.
9. **Security & data page** if asked about running strangers' code or where data lives.

## What is real today

- Mechanisms A (reproducibility) and B (holdout gap and leakage location) run end to end in production
  sandboxes: gVisor, no internet, UK region, holdout labels outside the sandbox.
- Employer workspace with roles, assessments, private candidate links, reviewer sign-off with reasons,
  feedback release, candidate contests, CSV export, audit log and retention deletion.
- One task (broadband churn) with planted traps and a holdout; 16 planted-defect benchmark submissions,
  all judged correctly in the latest logged run.

## What it does not claim yet

- Accuracy on real candidates' code: the benchmark was written by the build team. Pilot rounds will
  measure this.
- Resistance to AI-written submissions: not yet tested against Claude Code, Codex or Copilot.
- Learned scoring weights (Mechanism C): the rubric is hand-set and published; outcome data does not
  exist yet.
- An independent security review of the sandboxing.
- More than one task, GPU workloads, or integrations with applicant tracking systems.

---
name: developR
description: "Run a strict two-stage workflow for complex repository changes: plannR uses Opus 5.5 at high effort to clarify and design, then implementR agents, spawned by the root agent and all using Opus 5.5 at medium effort, implement and verify work items. Invoke explicitly for complex changes."
---

# developR

Run exactly two sequential stages. The root agent hands off between them, spawns the implementR workers, and reports results, but does not replace plannR.

## plannR — Opus 5.5 High

Spawn one subagent with model `claude-opus-5-5`, reasoning effort `high`, and `fork_turns: none`. Give it the complete request, constraints, acceptance criteria, workspace path, and repository instructions.

At the start, plannR must inspect only the relevant repository paths and identify the decisions it needs from the user about behavior, scope, priorities, tradeoffs, compatibility, and acceptance criteria. Turn each decision into a question with 2 to 4 concrete options, the recommended option first and labeled "(Recommended)". Never ask an open-ended question that expects the user to type an answer.

Because plannR runs as a subagent, the root agent presents these questions to the user with the AskUserQuestion tool, using up to 4 questions per call, each with its options. The user picks an option with radio buttons, or uses the "Other" option that the tool always provides to write a custom answer. The root agent passes the answers back to plannR and waits for them before plannR finalizes its plan. Do not silently choose material product, architecture, security, scope, or compatibility decisions. plannR may perform focused read-only inspection while answers are pending, but must not edit files or mutate external state.

plannR must produce a compact implementation contract grounded in the codebase. Choose the smallest viable change that fits the existing architecture. Explicitly avoid over-engineering, speculative abstractions, unnecessary layers, unrelated cleanup, premature generalization, and dependency upgrades. Resolve ordinary implementation details from repository evidence and leave no material alternatives for implementR.

For every ordered step, specify exact files and symbols, precise edits and rationale, expected behavior and acceptance examples, data/control flow where relevant, invariants and compatibility requirements, edge and failure handling, dependencies, targeted tests or checks, and nearby code or behavior that must not change. Include assumptions, regression risks, and exact final repository validation. plannR must not implement code.

After the user answers, plannR may ask one additional focused round only if new evidence exposes a material decision. Wait for the complete plan. The root agent performs only a lightweight feasibility check and sends at most one focused technical correction to plannR if needed.

## implementR — Opus 5.5 Medium

Only after plannR's plan is complete, the root agent spawns the implementR agents. Each one uses model `claude-opus-5-5`, reasoning effort `medium`, and `fork_turns: none`. Give each worker the original request, constraints, clarifications, workspace path, plannR's full plan verbatim, its assigned work items, precise file ownership, dependencies, checks, and the rules in this section.

Before spawning, the root turns each plan step into implementation work items and spawns as many implementR agents as the work needs. There is no fixed total limit; respect the available concurrency slots and schedule additional agents in batches as needed. Every implementR agent, including follow-up and replacement agents, must use Opus 5.5 Medium.

Workers must execute the plan as written, preserve unrelated work and existing behavior, and keep the change as small as possible. They must not independently redesign, over-engineer, refactor, clean up unrelated code, add dependencies, or introduce formatting churn.

Do not add code comments. Do not add docstrings, TODOs, or commented-out code. Preserve existing comments unless changed behavior makes them inaccurate; remove inaccurate comments without replacing them. Workers own non-overlapping files, run targeted checks after their changes, and do only their assigned work. The root schedules dependent work after prerequisites, reviews worker results, resolves conflicts narrowly, and dispatches focused follow-up workers when required.

After all planned work and targeted checks are complete, run the full repository suite exactly once, plus required project-specific linting, formatting, type-checking, generation, build, or other validation. If repository evidence requires a technical adjustment, make only the smallest change that preserves the intended behavior and record the deviation. Stop and report material product or authorization decisions rather than inventing them.

The final report, written by the root agent, must be concise and include workers and owned work items, files changed, behavior implemented, deviations, targeted checks, the final full-suite result, remaining risks, and one concise commit message summarizing the completed changes. Do not create the commit unless the user separately asks. The root agent may send one narrow correction request, which must go to an appropriately scoped worker.

## Coordination rules

- Ask the user questions at the start of plannR and obtain answers before finalizing its plan.
- Never start any implementR agents before plannR's complete plan is available.
- Never silently substitute the required models or reasoning levels.
- Prefer the smallest sufficient solution and explicitly reject over-engineering.
- Do not expose raw subagent transcripts or chain-of-thought.
- After the final report, ask the user whether to commit and push to main.
- No Claude Code attribution for the commit: no Co-Authored-By or "Generated with Claude Code" lines.

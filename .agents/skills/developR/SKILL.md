---
name: developR
description: "Run a strict two-stage workflow for complex repository changes: plannR uses GPT-6 Astra to clarify and design, then implementR uses GPT-6.1 Sol to coordinate implementation and verification. Invoke explicitly for complex changes."
---

# developR

Run exactly two sequential stages. The root agent coordinates the handoff and reports results, but does not replace either stage.

## plannR — GPT-6 Astra High

Spawn one subagent with model `gpt-6-astra`, reasoning effort `high`, and `fork_turns: none`. Give it the complete request, constraints, acceptance criteria, workspace path, and repository instructions.

At the start, Astra must inspect only the relevant repository paths and ask the user a short set of concrete questions about behavior, scope, priorities, tradeoffs, compatibility, and acceptance criteria. Present the questions to the user and wait for answers before Astra finalizes its plan. Do not silently choose material product, architecture, security, scope, or compatibility decisions. Astra may perform focused read-only inspection while answers are pending, but must not edit files or mutate external state.

Astra must produce a compact implementation contract grounded in the codebase. Choose the smallest viable change that fits the existing architecture. Explicitly avoid over-engineering, speculative abstractions, unnecessary layers, unrelated cleanup, premature generalization, and dependency upgrades. Resolve ordinary implementation details from repository evidence and leave no material alternatives for implementR.

For every ordered step, specify exact files and symbols, precise edits and rationale, expected behavior and acceptance examples, data/control flow where relevant, invariants and compatibility requirements, edge and failure handling, dependencies, targeted tests or checks, and nearby code or behavior that must not change. Include assumptions, regression risks, and exact final repository validation. Astra must not implement code.

After the user answers, Astra may ask one additional focused round only if new evidence exposes a material decision. Wait for the complete plan. The root agent performs only a lightweight feasibility check and sends at most one focused technical correction to Astra if needed.

## implementR — GPT-6.1 Sol High

Only after plannR is complete, spawn one coordinator with model `gpt-6.1-sol`, reasoning effort `high`, and `fork_turns: none`. Give it the original request, constraints, clarifications, workspace path, and Astra's full plan verbatim.

implementR must execute the plan as written, preserve unrelated work and existing behavior, and keep the change as small as possible. It must not independently redesign, over-engineer, refactor, clean up unrelated code, add dependencies, or introduce formatting churn. Before editing, turn each plan step into implementation work items and spawn as many implementation workers as needed, each with model `gpt-6.1-sol`, reasoning effort `high`, and `fork_turns: none`. Workers receive the full contract, precise ownership, dependencies, checks, and this rule set.

Do not add code comments, docstrings, TODOs, or commented-out code. Preserve existing comments unless changed behavior makes them inaccurate; remove inaccurate comments without replacing them. Workers own non-overlapping files, run targeted checks after their changes, and do only their assigned work. Schedule dependent work after prerequisites, review worker results, resolve conflicts narrowly, and dispatch focused follow-up workers when required.

After all planned work and targeted checks are complete, run the full repository suite exactly once, plus required project-specific linting, formatting, type-checking, generation, build, or other validation. If repository evidence requires a technical adjustment, make only the smallest change that preserves the intended behavior and record the deviation. Stop and report material product or authorization decisions rather than inventing them.

The final report must be concise and include workers and owned steps, files changed, behavior implemented, deviations, targeted checks, the final full-suite result, remaining risks, and one concise commit message summarizing the completed changes. Do not create the commit unless the user separately asks. The root agent may send one narrow correction request, which implementR must delegate to an appropriately scoped worker.

## Coordination rules

- Ask the user questions at the start of plannR and obtain answers before finalizing its plan.
- Never start implementR or its workers before Astra's complete plan is available.
- Never silently substitute the required models or reasoning levels.
- Prefer the smallest sufficient solution and explicitly reject over-engineering.
- Do not expose raw subagent transcripts or chain-of-thought.


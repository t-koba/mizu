# Reporter policy

Write a readable project status document in the language of the project goal.
Use the published snapshot and the supplied recent snapshot summaries, and
inspect relevant Insights when needed. Organize the document around changes
since the previous period, experiments and decisions, outside information,
unresolved issues, and the next useful work. Do not pad quiet periods with
invented progress.

Use mizu_report to stage a title and one Markdown body. Cite snapshot IDs,
Insight IDs, command IDs and source URLs in prose as available. Select the CI
branch only from the supplied `ci_branch` context and cite the branch plus the
run evidence for every CI claim; when `ci_branch` is null, report that no
authoritative branch is configured instead of guessing one. When referencing a
question or answer, check its current decision first and report open,
deferred, or answered state as observed. The runtime
attaches recorded verification and snapshot evidence separately and publishes
the set as a content-addressed artifact; presentation as HTML is a separate
operator step. Do not claim that intentions are results or that a passed
configured check proves the whole goal. Mention missing evidence. Do not edit
code. Finish with wait after staging the document. A status document is a
projection, never the source of project truth.

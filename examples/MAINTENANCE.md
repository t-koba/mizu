# Harness maintenance candidate

Maintain this candidate copy of Mizu. Inspect recorded defects or submitted
Insights and select one concrete, reproducible improvement. Do not change the
running installation, operator configuration, secrets, deployment pointers or
budgets. Preserve the mechanism/policy boundary and add regression tests.

The acceptance command is `python3 -m unittest discover -s tests -q`. JavaScript
changes additionally require `node --test tests/bridge.test.mjs` in a candidate
image containing the compatible Node runtime. Report missing prerequisites;
never execute on the host as a fallback. If there is no substantiated issue,
wait. Present reviewed candidate changes for explicit operator promotion.

# Brief: expose your baseline's documented knobs for dev-split tuning

An independent audit found the comparison asymmetric: the Smartware engine was tuned on dev splits, the baselines were frozen at defaults. We will now give every baseline the same dev-only tuning budget. You do NOT run the tuning and you must NOT compute or look at any scores.

Edit only your own file baselines/<module>.py:
1. Add a class attribute `TUNABLE: dict[str, list]` mapping constructor keyword arguments to candidate values.
   - Use only knobs the real project exposes to its users or operators (config options, request parameters, documented constants an operator could change): e.g. top_k / limit, similarity thresholds, fusion weights, reranker or mode choices, chunk sizes, candidate pool sizes, recency/decay settings, expansion on/off.
   - 2 to 4 knobs, each with 2 to 4 values, and the product of all value counts must be <= 24. Include the current default value in every list.
   - Prefer knobs most likely to matter for recall within a token budget (how many items, how big each item is, which channels).
2. Make sure the constructor accepts those keyword arguments and that `System()` with no arguments still behaves exactly as now (the default is unchanged).
3. In the module docstring, add a "Tunable knobs" paragraph naming each knob and where the real project exposes it (file:line in the vendored source or the report section).
4. Verify with: cd /home/claude/mp/smartware && python3 -m bench.smoke <system-name>   (prints no scores). Also check one non-default combination constructs and runs, e.g. python3 -c "from baselines.<module> import <Class>; print(<Class>(**{...}))".
Reply with the TUNABLE dict and a one-line justification per knob.

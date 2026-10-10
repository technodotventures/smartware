# Judge brief (blind QA v3, following LongMemEval's official grading rules)

Each item has a question, its question type, the gold answer, and a candidate answer written by a reader model from some retrieved memory context. Decide whether the candidate is correct.

General rule: CORRECT if the candidate contains the correct answer, or is equivalent to it, or contains all the intermediate steps needed to reach it. If it gives only part of a required answer, or contradicts the gold, it is WRONG. "I don't know", refusals and vague answers are WRONG.

Type-specific rules (from LongMemEval):
- temporal-reasoning: same rule, but do not penalise off-by-one errors in a number of days, weeks or months (if the gold is 18 days, 19 or 17 is CORRECT). The gold may list acceptable alternatives.
- knowledge-update: if the candidate mentions earlier information as well as the updated answer, it is CORRECT as long as the updated answer is the one it gives as the answer.
- single-session-preference: the gold is a rubric describing what a good personalised response looks like. CORRECT if the candidate recalls and uses the user's personal information or preferences in the way the rubric describes; it need not cover every point.
- all other types: the general rule.

Write a JSON object mapping uid -> "CORRECT" or "WRONG" to the output path given, using the Write tool, then reply "done N".

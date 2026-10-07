# Judge brief (blind)

Each item has a question, the gold answer, and a candidate answer produced by a reader model from some retrieved memory context. Grade each candidate:

- CORRECT if the candidate states the information in the gold answer. Paraphrases are fine. For dates, the same date is required at the granularity the gold uses (a gold "May 2023" accepts "7 May 2023"; a gold "7 May 2023" does not accept "May 2023"). For lists, the candidate must contain the main items of the gold. For counts or durations, the number must match (gold may list acceptable alternatives). Extra correct detail is fine; contradicting detail is not.
- WRONG otherwise, including "I don't know", vague answers, or answers about a different event.

Write a JSON object mapping uid -> "CORRECT" or "WRONG" to the output path given, using the Write tool, then reply "done N".

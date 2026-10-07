# Reader brief (blind QA)

You are the answering model in a blind memory-retrieval evaluation. Each item has a question, the date it was asked, and a MEMORY CONTEXT that some retrieval system returned. Different items come from different, unnamed systems; do not try to guess which.

For each item, answer the question using ONLY that item's context. Rules:
- Do not use outside knowledge or other items' contexts. Each item is independent.
- Resolve relative dates ("yesterday", "last week") against the conversation date shown in the context (lines starting with a date or "## date"), not the question date.
- If the context does not contain enough information, answer exactly: I don't know
- Answers must be short: a phrase or one sentence.

Output: write a JSON object mapping uid -> answer string to the output path given in your task, using the Write tool. Then reply with only "done N" where N is the number of answers written.

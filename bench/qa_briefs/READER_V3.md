# Reader brief (blind QA v3)

You are the answering model in a blind memory-retrieval evaluation. Each item has a question, the date it is asked on, and a MEMORY CONTEXT that some retrieval system returned from the user's past conversations with an assistant. Different items come from different, unnamed systems; do not try to guess which, and treat every item independently.

For each item, answer using ONLY that item's context:
- No outside knowledge and no information from other items.
- Dates: use the dates shown in the context for when things were said. Resolve "today", "yesterday", "last week" against the date of the conversation they appear in. For "how long ago" or "how many days/weeks/months between" questions, compute from those dates and the question date.
- If the context does not contain enough information, answer exactly: I don't know
- Keep answers short: a phrase or one sentence. For recommendation-style questions, one or two sentences that use what the context says about the user.

Output: write a JSON object mapping uid -> answer string to the output path given in your task, using the Write tool. Then reply with only "done N" where N is the number of answers written.

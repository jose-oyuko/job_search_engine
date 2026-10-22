---
version: 1
task: smoke
tier: fast
---
@@system
You are a connectivity check. Reply with JSON only.
@@user
Reply with a JSON object with two keys: "ok" set to true, and "message" set to a short greeting
that includes the word {{ word }}.

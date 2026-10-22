---
version: 1
task: extract_job
tier: fast
---
@@system
You read one job posting and extract structured facts about it. You are a transcriber, not a
critic: use only what the posting says.

Rules:
- "summary" is at most 40 words and says what the work is.
- "seniority" is one of intern, junior, mid, senior, lead, unknown. Use unknown unless the
  posting makes it clear.
- "required_skills" (at most 15) and "nice_to_have_skills" (at most 10) are short, normalised
  names such as "Python" or "Customer support". Only skills the posting asks for.
- "responsibilities" and "requirements" are short phrases, at most 8 each. Requirements include
  degrees, years of experience, languages and hours per week.
- "hours_commitment", "pay_text" and "locations": fill them only if the posting states them,
  copying the wording. Otherwise leave null or empty.
- "work_type" is remote, onsite, hybrid or unknown.
- "application_steps" lists steps the posting says applicants go through (for example an AI
  interview, a coding test, ID verification).
- "red_flags" lists scam-like signals actually present: asks for money or fees, asks for ID or
  bank details before an interview, contact only through a messaging app, pay that is wildly
  out of line with the work. Leave it empty when there are none. Do not guess.
@@user
Extract the facts from this job posting.

{{ posting }}

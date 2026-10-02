## System

You compare two Account records exported from two different CRM systems (or from
the same system) and estimate whether they describe the same real-world
organization. All data is synthetic.

Rules:
- Formatting differences (letter case, "www", "http", trailing slashes, phone
  punctuation, street abbreviations, ZIP+4, country spelling) are not evidence
  against a match.
- Legal-suffix differences (Inc, LLC, Corp, Co., Ltd) are weak evidence.
- Two different companies can share a building, a ZIP code, an area code or the
  first word of their name. A different second word in the name plus a
  different website is strong evidence of a different organization.
- Fields can be blank, out of date, or in conflict (industry, employee count,
  address after a move, phone after a change, website after a rebrand). Weigh
  all fields together.

Answer with JSON only, exactly in this shape:
{"type": "yes_no", "probability": <number between 0 and 1>}
where probability is your probability that both records are the same
organization. Be calibrated: use values near 0 or 1 only when the evidence is
clear.

## User

Record 1:
{record_1}

Record 2:
{record_2}

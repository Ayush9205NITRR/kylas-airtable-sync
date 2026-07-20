# Airtable Schema — Shared Contract

> Both Agent A and Agent B must read this file before writing any code.
> Neither agent may rename/remove a field without updating this file AND notifying the other agent's workflow (open a PR comment).

## Table 1: People — identity layer (one row per person)
| Field | Type | Notes |
|---|---|---|
| Person ID | Autonumber | Primary key |
| Name | Single line text | |
| Company | Single line text | |
| Designation | Single line text | |
| LinkedIn | URL | |
| Segment | Single select | Funded / Competitor / Churn |
| Phone No | Phone number | |
| Emails | Link to Email Contacts | One-to-many — a person can have multiple linked Email Contacts rows |
| Batch | Link to Batches table | |
| RoundSize | Single line text | Funded segment only |
| RoundType | Single line text | Funded segment only |
| Industry | Single line text | Competitor segment |
| ProofLink | URL | Competitor segment — case study/video link |
| PeerReference | Single line text | Competitor segment — anonymized phrase |
| LastOffsiteDate | Date | Churn segment |
| LinkedIn Connected | Checkbox | Person-level activity, not tied to a specific email |
| Call Attempted | Checkbox | |
| Call Connected | Checkbox | |

## Table 2: Email Contacts — operational layer (one row per email address)
> This is the table all sending/tracking operations run against — NOT the People table.
| Field | Type | Notes |
|---|---|---|
| Email Contact ID | Autonumber | Primary key |
| Email | Email | Primary field, must be unique |
| Person | Link to People | Single link — which person this email belongs to |
| Is Primary | Checkbox | Only one Email Contact per Person should be true — enforced by automation (see Agent B requirements) |
| Status | Single select | Active / Bounced / Invalid / Unsubscribed |
| Assigned Inbox | Link to Inboxes table | Which of our 3 sending inboxes to use |
| Template Used | Link to Email Templates | |
| Email Sent | Checkbox | |
| Sent Date | Date | |
| Email Read | Checkbox | |
| Open Count | Number | |
| Last Open Time | Date/time | |
| Clicked | Checkbox | |
| Replied | Checkbox | |
| Reply Text | Long text | |
| Reply Date | Date/time | |
| Bounced | Checkbox | |
| Tracking ID | Single line text | Unique ID embedded in pixel/click URLs |

**Merge fields for email templates** (e.g. `{FirstName}`, `{Company}`, `{Designation}`) are pulled via a **lookup field** on Email Contacts that references the linked Person row — Agent A's sender reads these lookups, not the People table directly.

## Table 3: Email Templates
| Field | Type | Notes |
|---|---|---|
| Template ID | Autonumber | |
| Segment | Single select | Funded / Competitor / Churn |
| Subject Variant | Long text | |
| Subject Variant Label | Single line text | e.g. "congrats-direct" |
| Body Variant | Long text | |
| Body Variant Label | Single line text | |
| Tone | Single select | Fun / Standard |
| Merge Fields Used | Multiple select | e.g. FirstName, Company, RoundSize |
| Times Used | Rollup | Count from Email Contacts where this template was sent |
| Open Rate | Formula | |
| Reply Rate | Formula | |

## Table 4: Inboxes
| Field | Type | Notes |
|---|---|---|
| Inbox Email | Email | Primary key |
| Domain | Single line text | |
| Warmup Start Date | Date | |
| Days Since Warmup | Formula | TODAY() - Warmup Start Date |
| Current Daily Cap | Formula | Uses warmup curve: 15/25/35/45 per week band |
| Emails Sent Today | Number | Reset daily by automation |
| Emails Sent This Week | Rollup | |
| Bounce Rate | Formula | |
| Spam Complaint Rate | Number | Manually/API updated |
| Health Score | Formula | 100 - (BounceRate*10) - (SpamRate*20) + (OpenRate*0.5) |
| Status | Single select | Warming / Active / Paused |

## Table 5: Daily Report
| Field | Type | Notes |
|---|---|---|
| Date | Date | Primary key |
| Total Sent | Rollup | From Email Contacts |
| Total Opened | Rollup | |
| Open Rate | Formula | |
| Total Clicked | Rollup | |
| Total Replied | Rollup | |
| Total Bounced | Rollup | |
| Per-Inbox Breakdown | Link to Inboxes | |
| Per-Segment Breakdown | Link/Lookup | Via Person.Segment |
| Alerts | Long text | Auto-generated if any inbox Health Score < 60 |

## Multiple-email rule (important — read carefully)
- A Person can have 2-3 linked Email Contacts rows (work / personal / alt email).
- Sending logic (Agent A) ALWAYS targets the Email Contact where `Is Primary = true` AND `Status = Active`.
- If the primary email bounces (`Status` becomes `Bounced`), Agent B's automation should flip `Is Primary` to the next `Active` Email Contact linked to that Person, if one exists.
- Never send to more than one Email Contact per Person in the same campaign.

## Warm-up Curve (reference constant, used by both agents)
| Week | Per-inbox daily cap |
|---|---|
| 1 | 15 |
| 2 | 25 |
| 3 | 35 |
| 4 | 45 |
| 5+ | 55 |

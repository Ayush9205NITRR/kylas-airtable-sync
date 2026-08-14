# Client Repository — sales intelligence on Enout's book

Everything else in this repo tracks the **pipeline** (who we are calling). This
tracks the **book**: the companies that have actually run an offsite with Enout,
split across ventures and macro industries.

It answers questions the pipeline tables cannot:

- What share of our clients are funding-raised ventures, and at which stage?
- Which macro industries have we sold into, and which have we never touched?
- What is the median round size of the ventures that buy from us?
- Which clients are enterprises/MNC-GCCs rather than startups?

```
modules/09_client_repo.py        build + report + push
utils/venture_classifier.py      the classification logic (pure, offline)
config/venture_taxonomy.json     the taxonomy — edit this, not the Python
config/venture_overrides.json    hand-researched funding data
scripts/setup_client_repo.py     one-shot Airtable schema
tests/test_client_repo.py        44 offline tests, no keys needed
```

## First run

```bash
python scripts/setup_client_repo.py --dry-run   # preview the 22 columns
python scripts/setup_client_repo.py             # create the table
python modules/09_client_repo.py --dry-run      # classify + print the split
python modules/09_client_repo.py                # write it to Airtable
```

The table lands in the **Company Database** base (`AIRTABLE_COMPANY_BASE_ID`),
alongside `Company List`. The GitHub workflow `client_repo.yml` runs it every
Monday 8:30 AM IST and also uploads the classified rows as a JSON artifact.

## What counts as a client

A company enters the repository when an offsite actually happened — not when it
reaches a hot stage. Three signals, any one of which is enough:

| Signal | Source |
|---|---|
| Account Status = `Offsite Done` | computed by `modules/06_account_health.py` |
| A contact at `Offsite Done` / `Offsite Done (Late Reachout)` | contact pipeline stage |
| A deal at `Won` / `Closed Won` / `Offsite Done` | deal stage |

Every row records *which* signal fired, in the **Client Basis** column. The
signals live in `client_signals` in the taxonomy — change them there.

## The venture split

| Venture class | Reads as a funded venture? |
|---|---|
| **Funded Venture** | ✅ |
| Listed / Public Company | |
| Public Sector / Government | |
| Non-profit / Academic | |
| MNC / GCC | |
| Agency / Services Firm | |
| Enterprise / Large Corporate | |
| Bootstrapped Startup | |
| Unclassified | |

Rules are evaluated in the order declared in the taxonomy and **the first match
wins**, so specific classes must sit above catch-alls. Funded ventures also get
a normalised **Funding Stage** (Pre-Seed → Series E+ / Growth-PE / IPO) and a
**Round Size (USD)** converted at the FX rates in the taxonomy — so a `₹40 Cr`
round and a `$5M` round are directly comparable.

### Evidence, and how much to trust it

Every verdict carries a confidence and a plain-English basis, so no row is a
black box:

| Confidence | Evidence |
|---|---|
| **High** | A manual override, or an explicit round type/size field on the record |
| **Medium** | A funding keyword in the Batch / Source of Data / name, or headcount over the enterprise threshold |
| **Low** | A legal-suffix or descriptive keyword — `LLP`, `Foundation`, `GmbH` |
| **None** | Nothing matched → `Unclassified` |

The report ends with a list of the Low/None rows, which is your worklist for
`config/venture_overrides.json`.

## Where funding data comes from

Kylas does not carry funding data, so the classifier reads it from wherever you
put it, in this order:

1. **`config/venture_overrides.json`** — hand research (Tracxn, Crunchbase,
   press). Keyed by Kylas company id (`by_id`, preferred — stable) or company
   name (`by_name`, case-insensitive). Always wins, always High confidence.
2. **A funding field on the record** — a Kylas custom field (`cfRoundType`,
   `cfRoundSize`, …) or an Airtable column on `Company List`. The candidate keys
   are listed in `signal_fields`; add your own there when you create the field.
   No code change is needed.
3. **Keywords** in Batch / Source of Data / name / description — e.g. a batch
   labelled "Funded Startups Jan-26".

## Macro industry

The raw Kylas `Industry` string is a long tail of hundreds of inconsistent
values. It collapses to 16 buckets plus `Other / Unclassified`:

> Software & SaaS · Financial Services & Fintech · E-commerce & D2C · Healthcare
> & Life Sciences · Media, Gaming & Entertainment · Education & EdTech ·
> Logistics, Mobility & Supply Chain · Manufacturing & Industrial · Energy,
> Climate & Utilities · Real Estate & Construction · Retail, FMCG & Consumer ·
> Travel, Hospitality & Food · Professional & IT Services · Telecom &
> Infrastructure · Agriculture & AgriTech · Public Sector & Non-profit

Two matching paths, in order:

1. **Exact match** of the raw industry string against a bucket's `raw` list.
   This is the curated path and beats every keyword — put new Kylas industry
   values here as you meet them. It is the precise, low-risk way to extend the
   taxonomy.
2. **Keyword match** over industry + company name + description, used only when
   no bucket claims the raw string.

`Industry Basis` on each row tells you which path produced the answer.

## Extending the taxonomy

`config/venture_taxonomy.json` is the single source of truth — the Python has no
hardcoded taxonomy, and `scripts/setup_client_repo.py` builds the Airtable
single-select options from it. So after adding a bucket or venture class:

```bash
python scripts/setup_client_repo.py    # re-run: adds the new dropdown options
python tests/test_client_repo.py       # guards order/definition consistency
```

Skipping the setup re-run means Airtable rejects writes of the new value — the
`AirtableClient` will warn and skip the column rather than fail the run, which
is quiet enough to miss.

## Sources

| Flag | Reads from | When to use |
|---|---|---|
| `--source airtable` (default) | `Company List` in the Company Database base | Normal operation. Account Status is already computed there and the sync runs twice daily, so this costs zero Kylas calls. |
| `--source kylas` | Kylas companies + contacts directly | When you want to bypass the Airtable sync state, or Airtable is behind. |

Other flags: `--dry-run` (classify + report, write nothing), `--report-only`,
`--test` (first 5 clients), `--json PATH` (dump the classified rows).

## Adding it to the daily sync

It is deliberately **not** wired into `run_sync.py` — the book moves slowly and
a weekly run is enough. To include it anyway, add to `run_sync.py`:

```python
print("\n" + "=" * 40 + "\nMODULE 9: Client Repository\n" + "=" * 40)
_load("09_client_repo.py").run(source="airtable")
```

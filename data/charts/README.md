# Award charts

One YAML file per chart-based program, named `<PointsYeah code>.yaml` (codes from `data/programs.yaml`).
`flypoints.providers.charts.ChartEstimator` reads every `*.yaml` here. Output is always flagged
`estimated=True`: a chart gives the saver *price*, never availability.

## Schema

```yaml
program: AC                  # must exist in programs.yaml and be reachable from Chase or Capital One
source: [url, ...]
verified: 2026-10-19
basis: distance | zone       # distance: price depends on zone pair + great-circle band; zone: zone pair only
distance_unit: miles         # distance charts only; the estimator uses statute miles
taxes_usd_typical: 150       # rough per-award taxes/fees guess (comment says how rough)
zones:                       # zone -> ISO2 country codes (airports.yaml supplies each airport's country)
  NA: [US, CA, ...]
domestic: {Y:, J:, F:}       # optional; price when origin and destination are in the same country
prices:                      # pair is unordered; same zone twice = within-zone
  - pair: [NA, ATL]
    bands:                   # basis: distance. up_to in distance_unit; the last band omits up_to
      - {up_to: 4000, Y: 32500, J: 60000, F: 90000}
  - {pair: [EU2, NA], Y: 55000, J: 90000, F: 135000}   # basis: zone: prices directly on the entry
```

Cabins are `Y`/`W`/`J`/`F`; omit a cabin the chart does not sell. All prices are one-way saver.
An airport whose country is in no zone, or a pair with no entry, yields no estimate.

## Adding a chart

1. Find the program's published chart (official page first; reputable blogs second) and cross-check
   a second source for at least some rows.
2. Write `<code>.yaml` per the schema. Do not fill in numbers you cannot source; leave them out.
3. Add test rows to `tests/test_charts.py` with the citation in a comment.

## Charts included

| Code | Program | Basis | Confidence |
|---|---|---|---|
| AC | Aeroplan partner (Star Alliance) chart | distance | Prices high (two sources agree where they overlap); Caribbean/Central America zone placement medium |
| TK | Turkish Miles&Smiles Star Alliance partner chart | zone | Prices high (two sources); zone membership of Puerto Rico/Caribbean low-medium |

## Deliberately excluded

- **Avianca LifeMiles, Air France-KLM Flying Blue**: dynamic pricing, no published chart.
- **British Airways Avios, Iberia Avios**: BA no longer publishes a chart; observed prices are
  origin-specific (London-based), peak/off-peak, and the third-party distance tables found disagreed
  with route examples. No clean source for non-London origins.
- **Singapore KrisFlyer (Star Alliance partner)**: the official chart is a PDF/image; only the
  Singapore-to/from column (1 Nov 2025) was found in text, not the full zone matrix needed for
  Caribbean-origin routes.
- **Virgin Atlantic Flying Club**: partner pricing (Delta, ANA) moved to distance/region schemes with
  conflicting third-party figures; not sourced cleanly.
- Everything else (Aeromexico, Cathay, Emirates, EVA, ...): not researched.

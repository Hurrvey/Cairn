# PDF visual baseline — October9

Coordinator inspected rendered source pages independently of parsed output. This is a small,
agent-reviewed reference, not a human-annotated30-document benchmark. Renders live under
data/acceptance-pdf-20261009; source identity/hashes remain in the September21 corpus manifest.

## financial_table_heavy-01

Source is the retained one-page DocLayNet excerpt, visually printed page82 of a Nissan annual
report. It contains a retirement-benefit-plan heading, explanatory prose, two financial tables,
intervening prose and a footer. Do not relabel the excerpt as a full report.

Baseline runner reports table_blocks0. Its Markdown separates right-side figures from their row
labels and appends large numeric sequences after body/footer text. Nonempty extraction is not a
valid table result. Visual row associations in the first table include:

| Row label | Left numeric column | Middle numeric column | Right numeric column |
| --- | --- | --- | --- |
| Retirement benefit obligation | (1,217,260) | (1,041,483) | (11,376,262) |
| Plan assets at fair value | 500,815 | 377,169 | 4,680,514 |
| Unfunded retirement benefit obligation | (716,445) | (664,314) | (6,695,748) |
| Unrecognized net retirement benefit obligation at transition | 120,718 | 131,666 | 1,128,206 |
| Unrecognized actuarial gain or loss | 154,689 | 152,867 | 1,445,691 |
| Unrecognized prior service cost | (66,720) | (61,833) | (623,551) |
| Net retirement benefit obligation | (507,758) | (441,614) | (4,745,402) |
| Prepaid pension cost | 445 | 652 | 4,159 |
| Accrued retirement benefits | (508,203) | (442,266) | (4,749,561) |

These are visual value anchors with currency symbols omitted deliberately; they do not score
full header hierarchy/cell spans. The rendered headers include multiple year/date/unit lines;
a quality engine must preserve associations instead of flattening them into body paragraphs.
The second table has row labels plus four numeric columns. Its Total row visually reads81,039,
86,179,129,036,757,374. A separate complete annotation is needed before calculating table F1.

## multicolumn-01

The source is a two-column academic page with equations, a plotted figure and its caption.
Left-column body continues vertically; right-column figure/caption and body are a separate
column. Existing report says table_blocks1, requiring inspection for a false table formed from
aligned formula fragments. Ordinary aligned columns alone are not table evidence. Formula
recognition is distinct from preserving readable text order and should not be silently claimed.

## Minimal independent layout reproductions

1. Full-width header, two body columns, full-width footer: current output puts footer between
   left and right columns. Expected header→left body→right body→footer.
2. Wide two-column table with labels at x40 and amounts at x430 on width600: current output is
   labels top-to-bottom followed by amounts, all paragraph. This is caused by the40% width veto.

Use these as review evidence and derive explicit fixtures before changes. They neither establish
general quality nor authorize replacing ambiguous prose with invented table cells.

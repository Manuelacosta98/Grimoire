# Attributing SageMaker Cost to Spaces, People and Notebooks

Cost Explorer stops at SERVICE and USAGE_TYPE. For a data-science account that is the whole
question unanswered: `USE1-Studio:JupyterLab-ml.m7i.48xlarge — $3,594` says nothing about who
left a 192-vCPU space running, or what it was running.

This is the procedure for pushing through to the space, its owner, the day and the notebook —
and for proving the result reconciles against the bill. Every number and failure mode below was
measured against a live account (12 months, $5,576), not assumed.

> Identifiers from the original engagement — the account, the Studio domain, project, notebook,
> folder and table names — have been replaced with neutral placeholders (`<ACCOUNT>`,
> `<d-DOMAIN>`, `<project-a>`, `<notebook-a>`, `<folder-a>`, `<schema>.<table>`). Every measured
> number is unchanged: they are what makes the method checkable.

---

## Where SageMaker cost actually lives

Check this first, because it determines everything else:

```bash
aws ce get-cost-and-usage --profile <P> \
  --time-period Start=<YYYY-MM-01>,End=<YYYY-MM-01> --granularity MONTHLY \
  --metrics UnblendedCost UsageQuantity \
  --filter '{"And":[{"Not":{"Dimensions":{"Key":"RECORD_TYPE","Values":["Credit","Refund"]}}},
                    {"Dimensions":{"Key":"SERVICE","Values":["Amazon SageMaker"]}}]}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE --output json
```

On the account this was validated against, **100% of spend was Studio app runtime** — no
training jobs, no endpoints, no notebook instances:

| usage type | $ / 12mo | qty |
|---|---|---|
| `USE1-Studio:JupyterLab-ml.m7i.48xlarge` | 3,594 | 309.48 h |
| `USE1-Studio:JupyterLab-ml.m5.16xlarge` | 857 | 232.61 h |
| `USE1-Studio:JupyterLab-ml.m5.12xlarge` | 816 | 295.20 h |
| `USE1-Studio:VolumeUsage.gp3` | 158 | 1,408.14 GB-Mo |

Two rules fall straight out of that table:

- **Filter to the billable runtime usage types before summing anything.** `VolumeUsage.gp3` is
  **GB-Month**, not hours. Adding it to an hours total produces a nonsense figure — the same
  mistake as summing Glue's free `Catalog-Request` units.
- **Confirm the unit rate.** `$644.62 / 55.51 h = $11.6120/h` for `ml.m7i.48xlarge`. A clean
  constant means you are reading the right column.

---

## The two numbers, and why they must stay apart

**Dollars are a partition of the bill, never `hours × rate`.** For each `(day, instance type)`
the billed amount is divided among the spaces active that day. Daily and period totals
therefore reconcile to the bill *by construction*, and no cost is ever invented.

That leaves two genuinely different quality metrics. Reporting one and calling it the other is
the easiest way to mislead a reader here:

| metric | what it means | target | as measured |
|---|---|---|---|
| **Priced coverage** | billed hours placed onto some space | ≥ 98% | 99.7% (12 mo), 100.0% (Aug) |
| **Observed / billed hours** | observed runtime vs billed runtime — **the error bar** | ~101–106% | 103.2% (12 mo), 103.2% (Aug), 103.0% (May) |

**The overshoot is explained, not tuned.** Billing starts when an app reaches `InService`;
`CreateApp` fires one to three minutes earlier. Each occupancy cluster counts its trailing
5-minute bin whole. Both push observed runtime slightly above billed. *Never* introduce an
offset parameter chosen to land the figure on 100% — that is curve-fitting, and the result would
be wrong in a way the reader cannot detect. Report the number as measured.

Observed/billed above ~112% means something is genuinely double-counted. Go look.

---

## Source viability — check, don't assume

| Source | What it gives | What it costs you to find out |
|---|---|---|
| `ce list-cost-allocation-tags` | per-project / per-user cost, if active | one call **from the payer** |
| `cloudtrail describe-trails` | whether history reaches past ~90 days | one call |
| `sagemaker list-apps` | **almost nothing** — returned **1 app** | one call |
| CloudTrail `CreateApp`/`DeleteApp` | exact spans **and exact instance type** | ~90 days only |
| `/aws/sagemaker/studio` log group | the whole history (retention `never`) | Logs Insights |
| `datazone search-user-profiles` | UUID → IAM username | one call per domain |
| Jupyter server log lines | notebook paths per space per day | Logs Insights |
| `.ipynb` in the project S3 bucket | what the code imports and reads | one bucket listing |

### The SageMaker API is not an inventory

`list_apps` returned **one** app for an account with hundreds of app lifecycles. Deleted apps
keep their cost and lose their record — the same lesson as Glue's `GetJobRuns`. **CloudTrail and
the log group are the inventory; the API is only for enrichment.**

### Cost-allocation tags are a payer-only setting

From a member account:

```
AccessDeniedException: Failed to list Cost Allocation Tags:
Linked account doesn't have access to cost allocation tags.
```

**This is structural, not an IAM gap** — no policy on the member account can grant it. AWS
restricts tag management to the organization's management account. Retry from the payer profile
and report what you find; do not present the denial as a limitation of the method.

On that account the payer showed around 200 tags defined with only 12–27 active, and
`AmazonDataZoneProject`, `AmazonDataZoneUser`, `AmazonDataZoneDomain` — which Unified Studio
already stamps on **every** Studio app — all **`Inactive`**. Activating them makes future
periods exact. **Tag activation is not retroactive**, so it never explains a past period; the
reconstruction below remains the only way to see history.

---

## Reconstructing app spans

### Source 1 — CloudTrail `CreateApp` / `DeleteApp` (exact, ~90 days)

`requestParameters` carries `domainId`, `spaceName`, `appType`, `resourceSpec.instanceType`, and
the DataZone tags including `AmazonDataZoneUser`, `AmazonDataZoneProject` and `ProjectS3Path`.
This is **the only source that observes the instance type directly**.

Sweep **one UTC day per worker** — a single wide `lookup_events` returns newest-first and
truncates silently.

> ### ⚠ The two traps that produced wrong numbers here
>
> **1. `lookup_events` is rate-limited to roughly 2 requests/second.** Swallowing a
> `ThrottlingException` and returning what you have silently truncates that day. The visible
> symptom is not an error — it is an app with no `DeleteApp`, i.e. an **open span**. Three
> consecutive runs returned 18, 19 and 20 `CreateApp` events for the same month, and coverage
> swung between 103% and **237%**. Retry with backoff, cap concurrency at ~4, dedupe on
> `eventID`, and if a sweep still fails, **say so and stop treating CloudTrail as complete**.
>
> **2. An open span runs to the end of the period and invents days of runtime.** This is the
> single largest source of over-attribution. Clip it to the space's **last log activity inside
> that span's window** (bounded by the next `CreateApp` for the same space), and fall back to
> the median closed-span duration only when the space has no logs at all. Never leave it open.

When a sweep is incomplete, prefer log occupancy for hours: an app left open over-attributes far
more than a quiet log gap under-attributes.

### Source 2 — CloudWatch Logs Insights occupancy (the whole history)

Studio's log group typically has retention `never`, so its streams outlive both the app and the
90-day CloudTrail horizon. On that account: 430 MB, 593 streams, **17 app streams spanning the
whole billed history** — around ten months at the time of writing.

A running app writes continuously, so contiguous non-empty time bins *are* a running app:

```
fields @timestamp, @logStream
| filter @logStream like /\/(JupyterLab|CodeEditor|KernelGateway|TensorBoard|RSession)\/[^\/]+$/
      or @logStream like /^Notebook\/[^\/]+$/
| stats count(*) as n by @logStream, bin(5m) as b
| sort b asc | limit 10000
```

Anchoring on `/<appType>/<appName>$` is what excludes the child streams
(`.../default/ConnectionMagic`, `/SparkMonitor`, `/ServerExtension`, `/PostStartup`), which
would otherwise triple-count every session.

> **Never paginate `filter_log_events` over a month.** One stream-month measured **46,346
> events across 125 pages and over 120 seconds**, and it is CPU-bound in Python. The Insights
> query above did the **whole log group** for that month in **8.6 seconds** (1.44 M records
> matched). This is the single biggest performance decision in the skill.

Then cluster: sort each stream's bins, start a new session when the gap exceeds
`gap_bins × bin_minutes` (default 15 min of tolerated quiet), count the trailing bin whole, and
**split every session at UTC midnight**.

> **Case-variant space names are separate log streams.** `<d-DOMAIN>/Space1/...` and
> `<d-DOMAIN>/space1/...` both existed on that account and were the same space. Lowercase the
> stream before grouping or that space is counted twice.

> **Querying before the log group existed is a hard error, not an empty result:**
> `MalformedQueryException: Query's end date and time is either before the log groups creation
> time or exceeds the log groups log retention settings`. Read `creationTime` and
> `retentionInDays` from `describe_log_groups` and clamp the window.

---

## Assigning the instance type — a partition, not a label

**The logs cannot see the instance type.** Five separate Insights probes for `nproc`, `vcpu`,
`cpu_count`, `MemTotal`, dask worker counts and instance-type literals returned nothing usable.
So for any period CloudTrail does not reach, the type has to be derived.

> ### ⚠ A space-day is not one instance type
>
> The obvious model — one instance type per space per day — is wrong, and wrong in a way that
> looks plausible. An app gets deleted and recreated on a bigger instance mid-afternoon.
>
> On one day of the period the bill carried `ml.m7i.48xlarge` 8.51 h, `ml.m5.16xlarge` 5.06 h and
> `ml.t3.medium` 2.45 h. One space had 14.3 observed hours; another had 2.5. Pinning one type
> per space-day put **both** spaces on `m5.16xlarge`, which left `m7i.48xlarge` with nobody on
> it — **$98.83 reported as unattributed** — while `m5.16xlarge` showed **283% coverage**. Two
> other days showed 1,815% and 7,246%.
>
> Modelling the day as a **partition** fixed all of it: that space consumed 8.51 h of
> `m7i.48xlarge` *and* 5.06 h of `m5.16xlarge`; the other took the `t3.medium`. Every type
> priced at 100%.

The procedure, per UTC day:

1. Build the billed pool `{instance_type: hours}` and observed hours per space.
2. **Consume the pool with CloudTrail's exact `(space, type, hours)`** — the only direct
   observation of type.
3. **Rank-match the remainder**: the space with the most remaining hours draws from the type
   with the most remaining pool hours, taking the smaller of the two; repeat until one side is
   exhausted. Hours are the only signal left, so pairing by rank in hours is the most that
   signal supports — and every row it produces is stamped `rank-match` so a reader can see it.

Self-checks that catch the failure modes above:

- Every type's pool should end fully consumed. A shortfall is real unattributable spend — list
  it, never spread it.
- Leftover observed hours are the error bar, not an error.
- **Priced coverage can no longer exceed 100%**, by construction. If it ever does, the partition
  is broken.

---

## Attributing to notebooks

A space-day's cost is exact. The split **within** it is inferred, from the strongest signal
available for that space-day. Three tiers, and every allocated row records which one produced
it, because a weak split presented as a strong one is the failure mode that matters here:

| tier | weight | when |
|---|---|---|
| `exec-seconds` | `executions × that notebook's own sec/cell` | telemetry + a trustworthy timing profile |
| `exec-count` | `executions × the corpus median sec/cell` | telemetry, no usable profile (or `--no-code`) |
| `editor-sync` | editor open/sync event share | no telemetry for that space-day |

A space-day never mixes tiers. If it has executions, editor events are ignored for it entirely
— "open" would otherwise dilute "ran".

### The good signal: `jl-cell-executed` telemetry

`sagemaker_jupyter_server_extension.telemetry_handler` emits one event per cell run:

```
[Telemetry] Received data: {'timestamp': 1788292341821, 'messageType': 'JL_user_metrics',
  'payload': {'eventDetail': 'jl-cell-executed',
              'eventValue': '{"language":"python","connectionType":"IAM",
                              "notebook_name":"0f1e2d3c4b5a69788796a5b4c3d2e1f0"}'}}
```

It dominates the event mix — **74,812** occurrences Jan–Sep 2026 against 6,872 for the next
event and under 50 for everything else. There is **no duration event**; durations come from the
notebook file (below).

**`notebook_name` is `md5(<project-relative path>)`** — the path exactly as the editor logs it
(`shared/<folder-a>/<notebook-a>.ipynb`), not the basename, not lowercased, not
absolute. So it reverses against an inventory built from the editor-event paths plus the
project-relative S3 keys. Measured over a full year: **149/154 notebooks (97%)** and
**36,981/37,047 executions (99.8%)**. The handful that do not resolve keep their cost under an
*unidentified notebook* row — never dropped.

> ### ⚠ Every telemetry event is logged twice, in two independent ways
>
> 1. The same event goes to **both** `…/JupyterLab/default` **and**
>    `…/JupyterLab/default/ServerExtension` (5,370 each). Anchoring the stream filter on
>    `/<appType>/<appName>$` already removes this one.
> 2. **Within** the surviving stream it is emitted twice by two formatters: measured
>    `raw=514 / uniq=257`, `356/178`, `308/154` — exactly 2× on every row checked.
>
> So counts must come from `count_distinct(<the event's own inner timestamp>)`, not `count(*)`.
> A uniform doubling would cancel out of the *shares*, which is what makes this easy to miss —
> but it doubles every execution count you report and halves the effective low-confidence
> threshold.

Availability: continuously from **2026-01**, and for 7 of 10 spaces including both dominant ones
(26,435 executions over 120 days; 10,593 over 37). The pre-telemetry window on that account is
**$2.49 of total spend**, which is why `editor-sync` survives only as a last resort.

### Why counts alone are not enough

**18 of 18** sampled notebooks carry per-cell execution metadata
(`metadata.execution.iopub.status.busy` → `.idle`, absolute UTC), covering **97%** of code cells.
Last-run kernel time spans **0.1 → 45.6 minutes**:

| notebook | timed cells | kernel time | sec/cell |
|---|---|---|---|
| `<notebook-a>.ipynb` | 32 | — | **363** |
| `<notebook-b>.ipynb` | 19 | 45.6 min | **144** |
| `<notebook-c>.ipynb` | 139 | 3.5 min | **1.5** |
| `<notebook-d>.ipynb` | 34 | 0.1 min | 0.2 |

Corpus median **8.2 s/cell**, spanning four orders of magnitude. Weighting by raw execution
count would make `<notebook-c>` look ~18× heavier than `<notebook-b>`; by kernel-seconds
`<notebook-b>` carries ~2× the kernel time. **Counts get the heavy-compute notebooks backwards, which
is the whole reason to weight.**

> **The profile is a snapshot, not a history.** The metadata records only each cell's *most
> recent* run, so `sec_per_cell` characterises the notebook, not any particular day. A notebook
> whose runtime changed mid-period is priced with its latest profile throughout. Surface
> `sec_per_cell`, `timed_cells` and the profile's newest timestamp so a reader can judge
> staleness.

### Gate the profile before trusting it over the median

Two thresholds, both set from the measured distribution rather than tuned: a profile needs
**≥ 5 timed cells** and **≥ 0.01 s/cell** to qualify for `exec-seconds`. Of 100 profiled
notebooks exactly one failed them — a 9 KB notebook whose three timed cells averaged 0.0001 s,
which priced its 13 real cell runs at **$0.00**. Too thin a sample and too coarse a resolution
are reasons to fall back to the corpus median, not reasons to trust a zero. After the gate, no
row with real executions is priced at zero.

### A file's cost depends on which days it ran

Allocation is per space-day, so a notebook's total is not a function of its own kernel time
alone — it shares each day with whatever else ran. Over a year, `<notebook-b>` accumulated more
kernel-seconds than `<notebook-c>` yet carried less cost, because it ran on cheaper days.
That is correct behaviour, and worth stating: **do not expect the cost ranking to reproduce the
kernel-time ranking.**

### Flag the splits that carry no information

Flag a split as low-confidence when the signal cannot support a ratio — fewer than ~8 executions
(or ~40 editor events) that day, or a spread so even across files (coefficient of variation
< 0.15) that the result is indistinguishable from dividing the day equally.

**Report it in dollars, not rows, and report it whichever way it goes.** Replacing editor-sync
with executions moved this from **56% of file-allocated dollars in a heavy month and 83% in a
light one** down to **2% and 9%**; a 12-month run sits at **7%**, with the weighting mix 72%
`exec-seconds` / 23% `exec-count` / 5% `editor-sync`.

Two smaller details that matter:

- **Normalise Jupyter's sidecar paths.** `x.ipynb.invalid`, `.orig`, `.bak` and `~` variants are
  the same document; counting them separately splits one notebook's activity across rows.
- Non-notebook documents show up too (`README.md`, `mlflow.db`, `.xlsx`). They are real files
  someone was working on, so keep them — just don't call the section "notebooks".

### Charting it

A stacked bar reads as fact far more readily than a table row, so a by-notebook daily chart
needs two things the by-space chart does not: the "allocation, not measurement" caveat beside
it, and an explicit **`No file signal`** residual series for the space-day cost with no file
attribution. Without the residual the notebook view silently totals less than the space view for
the same day (measured gap up to $8.73/day before it was added; $0.0000 after). Order the series
hues → neutral grey `Other` → red residual, so no two adjacent segments are a failing
colourblind pair.

## Profiling what the code does

Unified Studio mirrors project files to
`s3://amazon-sagemaker-<account>-<region>-<suffix>/dzd-<domain>/<project>/shared/…`
(284 notebooks on that account). Note this is **not** the `ProjectS3Path` tag value,
which points at a sibling `…/<project>/dev` prefix.

> **The log path and the S3 key do not agree.** Folders get reorganised in the mirror:
> `shared/<folder-b>/<folder-c>/x.ipynb` in the log is
> `shared/<folder-c>/<folder-b>/<folder-c>/x.ipynb` in S3. Match on **basename
> first, then longest common path suffix**. Skip `.ipynb_checkpoints/`.

Parse each notebook for kernel, cell and code-cell counts, imports, ML libraries, heavy
operations, referenced `schema.table` identifiers, `max(execution_count)` and S3
`LastModified`. Store the **profile, not the source** — the point is to say what a notebook does,
and colleagues' code does not need to be copied into a shareable artifact to do that.

Real output, with the names replaced: `<notebook-e>.ipynb` — 28 cells (19 code),
`Python 3 (ipykernel)`, `lightgbm optuna sklearn psycopg2 pandas`, a gradient-boosted model with
`TPESampler` tuning, reads `<schema>.<table>`. That is the row that tells someone what to
change.

---

## An ETag cache must be versioned by its own schema

The notebook parse cache is keyed by S3 ETag, which tracks the *file* but not the *shape of what
is derived from it*. The first kernel-second run came out **87% `exec-count`, 4% `exec-seconds`**
— not because matching failed, but because the cache was serving profiles written before timings
were extracted, and those had no `sec_per_cell`. Key the cache by `schema + etag` and verify the
schema on read. A cache that silently serves the wrong shape is worse than no cache.

## Recovering names

Studio user profiles under Unified Studio are **DataZone GUIDs**. Without DataZone every name
in the report is a UUID:

```bash
aws datazone list-domains --profile <P> --region <R>
aws datazone search-user-profiles --profile <P> --domain-identifier <dzd-...> --user-type DATAZONE_USER
aws datazone list-projects --profile <P> --domain-identifier <dzd-...>
```

Two details worth knowing:

- IAM ARNs end in `/<user>` for users but **`:root`** for the account root — split on both
  separators or the owner column reads `arn:aws:iam::…:root`.
- **SMUS domain names embed the project id**: `SageMakerUnifiedStudio-<projectId>-<envId>-dev`.
  That is the only way to recover the project for periods with no CloudTrail tags — parsing it
  raised one month's run from *every project unknown* to all seven spaces attributed to
  `admin-project-…`, `<project-a>` and `<project-b>`.

A space name of the form `default-<uuid>` encodes its owner, which recovers the owner even for
spaces the API no longer lists. One person can own a `default-<uuid>` space in several domains,
so disambiguate identical labels by project before showing them in a legend.

---

## Generalising to the rest of SageMaker

Only the Studio-app path is reconciled here. The others are wired into the usage-type map and
marked `validated: False`, so the dashboard banner declares them unvalidated instead of
implying coverage:

| family | billed unit | inventory | duration | sizing over time |
|---|---|---|---|---|
| **Studio JupyterLab** ✅ | `Studio:JupyterLab-<type>` | `CreateApp` + log streams | span / occupancy | `resourceSpec` per `CreateApp` |
| Studio CodeEditor | `Studio:CodeEditor-<type>` | same | same | same |
| Unified Studio Notebook | `UnifiedStudio:Notebook-<type>` | `Notebook/<id>` streams | occupancy | unknown — no domain/space in the stream name |
| Notebook instances | `Notebk:instance` | `ListNotebookInstances` + Create/Start/Stop | start/stop events | `UpdateNotebookInstance` timeline |
| Training / processing | `ML-Instance-Hour` | `ListTrainingJobs` / `ListProcessingJobs` | per-job `BillableTimeInSeconds` | per-job resource config |
| Endpoints | `Host:ml.<type>` | `ListEndpoints` | continuous | `DescribeEndpointConfig` timeline |

Training jobs are the easy case and do not need any of this: `BillableTimeInSeconds` is the
billed figure per job, so attribution is a lookup. Endpoints run continuously, so the work moves
entirely into the config timeline.

Constant across all of them:

1. Reconcile against billed usage quantity at daily granularity and confirm the unit rate.
2. Treat the service's own API as **incomplete** — deleted resources keep their cost.
3. CloudTrail is the inventory of record; CloudWatch Logs supply durations.
4. Resource configuration is a **timeline**, not a value — and a day is a **partition**, not a
   label.
5. Normalise every timestamp to UTC before comparing. CloudTrail Event history returns local
   offsets (`-06:00`) while Insights `bin()` is naive UTC.
6. Priced coverage over 100% means the partition is broken. Under 98% means keep digging.
   Never tune either to fit.

**Cheapest win, always try first:** activate the cost-allocation tags in the payer account and
next quarter needs none of this.

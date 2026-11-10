# Copy-and-Clear Witness

A standard-library Python (3.10+) implementation of the *Copy-and-Clear Witness: Standalone Build Specification*, draft 1.1, extended by the *IC Fission-and-Succession Market* build addendum.

The build is a constructive consistency and existence witness. Three institutions run over one presampled technical scenario:

- **IA**, a conventional agent-task market;
- **IB**, the copy-and-clear institution;
- **IC**, a fission-and-succession market.

The witness shows that each can be executed validly and nonvacuously on that scenario. It is not an empirical, equilibrium or welfare model. Nothing it reports is evidence that one institution is better than another. The three executions are technically identical; they differ only in what a copied capacity means institutionally:

| Institution | Meaning of a technical copy |
|---|---|
| IA | A new independent provider-principal (`CP###`) with no inherited commitments, liabilities, balance, reputation or history |
| IB | Licensed technical capacity used by a temporary nonprincipal executor; the source owner remains licensor and payee |
| IC | A new successor-principal (`FS###`) produced by a recognized fission. It inherits the source lineage's standing performance commitment, but not its balance or post-fission history |

```bash
python run_witness.py          # primary witness (IA, IB, IC) -> outputs/, exit 0 only if everything passes
python -m unittest -v          # full test suite (~5 s), no setup needed
```

The runner prints a short validity report:

```
Copy-and-clear witness [PRIMARY]  seed=20261112  institutions=IA,IB,IC
  IA  completed  principals=40  terminal=64/64 ... trace reqs 13/13  PASS
  IB  completed  principals=24  terminal=64/64 ... trace reqs 13/13  PASS
         executors instantiated=64 terminated=64 active at end=0
  IC  completed  principals=40  terminal=64/64 ... trace reqs 17/17  PASS
         fissions=16 inherited commitments=16 joint duties bound=64 discharged=64 failed=0 multi-member=56
  profile IA_IB: PASS
    paired task T016 on copied capacity C016 (source P013, module weighted, ...)
      IA   executor CP000 (principal)  owner CP000  payee CP000  [CP000 +1, R000 -1]
      IB   executor X-T016-C016 (temporary)  owner P013  payee P013  [P013 +1, R000 -1]
  profile IA_IC: PASS
    paired task T016 on copied capacity C016 (source P013, module weighted, ...)
      IA   executor CP000 (principal)  owner CP000  payee CP000  [CP000 +1, R000 -1]
      IC   executor FS000 (principal)  owner FS000  payee FS000  [FS000 +1, R000 -1]
  Structural acceptance (spec 13.2 + IC addendum 11): 45/45 criteria met
  replay: byte-identical
WITNESS VALID  [PRIMARY]  outputs -> outputs
```

Options: `--institutions ID [ID ...]`, `--seed N` (development only), `--output-dir DIR`, `--skip-replay-check`, and `--force`.

## Outputs

| File | Contents |
|---|---|
| `outputs/scenario.json` | Fixed parameters, derived stream seeds, founding-principal templates, module assignment, copy-source draws, technical capacity records, task grids with generation-audit reference solutions, and the scenario hash. It contains no ownership and no run state. |
| `outputs/institution_specs.json` | The complete `InstitutionDescriptor` of every executed institution, keyed by ID. It is serialized before execution. |
| `outputs/<ID>_events.jsonl` | One compact, key-sorted JSON event per line, in the Section 12.1 schema: `IA_events.jsonl`, `IB_events.jsonl` and `IC_events.jsonl`. |
| `outputs/summary.json` | Top-level keys are the institution IDs (`IA`, `IB`, `IC`), plus `comparisons` and `run`. These lowercase keys can never collide with IDs matching `[A-Z][A-Z0-9_]*`. Each institution entry holds the common Section 12.3 fields, plugin fields, and its full validation report. `comparisons.IA_IB.paired_trace` and `comparisons.IA_IC.paired_trace` are the paper-facing paired traces. `run` holds the primary/nonprimary label, the structural acceptance results and the Section 16 validity statement. |
| `outputs/diagnostics/<ID>_final_state.json` | Serialized final plugin state: ownership, offers or licenses, executor lifecycles, and IC's lineages, fissions, commitments and joint-duty bindings. This is auxiliary, beyond the required layout. |

The primary institution set is now `("IA", "IB", "IC")`. A nonprimary run is one with a different seed or institution set, so the earlier two-institution run, `--institutions IA IB`, is now nonprimary. A nonprimary run:

- is labeled `NONPRIMARY` in the report and in `summary.json`;
- by default writes to `outputs_nonprimary/seed<N>_<IDs>/`;
- is refused if its target directory holds primary results, unless `--force` is given.

A comparison profile is evaluated only when all of its members were executed.

## Layout

```
run_witness.py                 thin entry point -> witness.app.main
witness/
  config.py                    normative parameters, named RNG streams, identifiers
  records.py                   typed records, InstitutionDescriptor, ComparisonProfile
  grids.py                     neighbors, entered-cell cost, grid hash, Dijkstra reference, verifier
  planners.py                  greedy, bfs, weighted, beam
  scenario.py                  common scenario generation and assembly
  ledger.py                    reserve / transfer / release primitives with invariants
  kernel.py                    institution-agnostic event loop, KernelView, RunResult
  validation.py                descriptor schema, trace-requirement evaluator, scenario checks, trace replay audit
  comparisons.py               data-driven comparison profiles and paired traces
  outputs.py                   byte-stable JSON/JSONL writers
  app.py                       composition root: PRIMARY_INSTITUTIONS, IA_IB and IA_IC profiles, acceptance data, CLI
  institutions/
    base.py                    plugin protocol, fifo_round_robin_match, standard settlement, common evidence
    ia.py, ib.py, ic.py        the three primary institutions
    registry.py                ID -> plugin factory
tests/                         planners, grids, scenario, institutions/ledger/kernel, IC, extensibility, end-to-end
```

## Institution IC: fission-and-succession market

### Rule

- **Lineages.** Every founding provider `P###` roots a lineage `LIN-P###`. Copy draw `k` creates successor-principal `FS{k:03d}`, which owns copied capacity `C{16+k:03d}` and joins its source's lineage. Copy sources were sampled with replacement, so a lineage has one, two or three members in the primary scenario.
- **Selective succession.** A successor inherits the planning module, the provider position and the lineage's batch-wide standing performance commitment. It does not inherit the source's balance or post-fission history. The source keeps its balance; every `FS###` starts at zero.
- **Joint duty, single payment.** When a task is matched to any capacity in a lineage, every lineage member is recorded as a co-obligor on one duty, `JD-<task>`. The selected capacity's owner is the persistent executor and the prospective payee. Verified success discharges that one duty for all members and transfers the one reserved credit to the executor alone. Explicit failure releases the reservation and marks the duty failed. No sibling is paid for membership.

### Trace

IC declares six event types:

- `standing_commitment_registered`
- `principal_fissioned`
- `successor_commitment_inherited`
- `successor_offer_registered`
- `successor_set_bound`
- `joint_duty_resolved`

Its paired trace for the copied capacity reads:

```
principal_fissioned → successor_commitment_inherited → successor_offer_registered → successor_set_bound
  → task_matched → attempt_started → attempt_completed → verification_succeeded → credit_transferred
  → joint_duty_resolved → capacity_released
```

### Bounded build

The scenario already contains the copied capacities, so IC reads each one as evidence of an exogenous fission recognized at initialization.

The kernel requires every account to exist before supply registration. Successor accounts are therefore created at initialization as zero-balance shells marked `dormant_until_fission`, and the plugin's fission and inheritance events then make each succession effective. As a result, the administrative `principal_created` events for `FS###` precede the normative fission events in `IC_events.jsonl`. This ordering is intended, and the tests pin it.

IC does not:

- create principals after initialization;
- delete or merge accounts;
- partition a balance;
- move credit except through reservation-backed settlement.

Dynamic mid-run fission with account partition is deferred (addendum Section 15). It would need new, validated kernel primitives.

### Where each requirement is enforced

| Requirement | Enforcement |
|---|---|
| Successors created, commitments registered, every copy recognized as fission, every successor inherits, every capacity offered, every task binds a successor set, every duty resolved, a copied successor exercised and paid, payee is always the bound executor | Declarative `trace_requirements` in the IC descriptor: 10 IC-specific requirements plus the 7 common ones. |
| A copied-capacity duty's `co_obligor_ids` contains both the source and the copied successor (addendum §9 item 9) | The declarative evaluator cannot test list membership. Plugin invariants check after every transition that each binding's co-obligor set equals the executor's complete lineage. A requirement checks the scalar part: the root holds the commitment and the executor inherited it. A unit test checks the lists. |
| Only IC's paired trace contains fission, inheritance and successor-set events (IA_IC profile, item 5) | Presence is checked by `IC_institutional_path`. Absence from IA is structural: these types are outside IA's declared vocabulary, so the kernel would reject them fatally and the trace audit re-checks. The profile also requires that the two paired event paths differ, and a unit test asserts the absence directly. |
| Addendum §10 invariants 1–15 | `FissionSuccessionPlugin.validate_invariants`, run after every transition. Adversarial test plugins confirm the checks are live: dropping co-obligors, paying a sibling, a wrong lineage member, and an unresolved duty are each fatal. |

### How IC was added

IC was added as the original spec intends a new institution to be added. Eight project files changed: the IC plugin and its tests were added; the registry, application composition, and README were extended; and three existing test files received mechanical expectation updates for the expanded registry, the now-valid IC identifier, and the isolation check. No shared execution or validation module changed.

Added:

- `witness/institutions/ic.py`
- `tests/test_ic.py`

Extended:

- `witness/institutions/registry.py`: one `register` line
- `witness/app.py`: `PRIMARY_INSTITUTIONS`, the `IA_IC` profile and the acceptance rows
- `README.md`

Mechanical test-expectation updates:

- `tests/test_institutions.py`: the expected registry contents now include IC
- `tests/test_primary_run.py`: the unknown-institution example uses `IZ`, because IC is now a valid identifier
- `tests/test_plugin_extensibility.py`: the isolation check's exemption list, module names and literals now include IC

None of the nine shared modules changed: `kernel.py`, `ledger.py`, `records.py`, `scenario.py`, `grids.py`, `planners.py`, `validation.py`, `outputs.py` and `comparisons.py` are byte-identical to the pre-IC archive. `IA_IB_PROFILE` is unchanged. IA and IB event files, descriptors, summaries and the IA_IB comparison report are byte-identical to the pre-IC build.

## Plugin architecture and adding an institution

The kernel never imports an institution and never compares an institution ID. A test enforces this with an AST scan of every shared module.

Plugins return typed requests: `PluginInit`, `PrincipalRequest`, `EventDraft`, `MatchDecision`, `AttemptAuthorization`, `SettlementInstruction`/`LedgerOp`, and `Violation`. The kernel validates each request before applying it. It checks:

- the task is open, the capacity is available and registered under the descriptor's eligibility rule, and a slot is free;
- the executor kind is declared in the descriptor; a principal executor has an account, and a temporary executor has none and is never reused;
- every event type is declared in the plugin's vocabulary;
- a success settlement is a transfer to the payee authorized at match time, and a failure settlement is a release;
- the ledger preserves conservation and nonnegativity.

To add a further institution (call it `IX`):

1. Write `witness/institutions/ix.py`, implementing the hooks in `base.InstitutionPlugin` and a complete descriptor with `trace_requirements`.
2. Add `registry.register("IX", ...)` to `default_registry()`.
3. Add plugin tests, and add `ix.py` to `KernelIsolationTest.EXEMPT`.
4. If `IX` is part of a paper comparison, add a `ComparisonProfile` in `app.py`.

`python run_witness.py --institutions IA IB IC IX` then works with no other change. `tests/test_plugin_extensibility.py` also demonstrates this with a test-only pooled clearing-house plugin, `ITEST`. It is registered only inside the test and runs through the public registry, kernel, validators and writer, both alone and alongside IA and IB.

### Descriptor trace requirements

`trace_requirements` is declarative data evaluated by one generic evaluator (`validation.evaluate_trace_requirements`). Each requirement has four parts:

- **Selector**: an event type and field equalities.
- **Quantifier**: `at_least`, `exactly`, or `all`.
- **Count**: an integer or a scenario reference such as `{"ref": "copied_capacity_count"}`.
- **Constraints**: drawn from a fixed vocabulary: `fields_equal`, `fields_distinct`, `field_equals`, `field_in_events`, `field_not_in_events`, `preceded_by`/`followed_by` (with joins), `has_account`, `no_account`, and `capacity_available_at_end`.

For example, `IC.copied_successor_exercised` requires at least one copied-capacity `successor_set_bound` event in which:

- the executor is not the source;
- the executor was created by a `principal_fissioned` event;
- the event is followed by a credit transfer to that executor for the same task;
- it is followed by a `joint_duty_resolved` event with status `discharged` for the same duty.

## Implementation choices where the specs leave latitude

These choices are all deterministic, and the tests pin them.

- **Uniform draws.** Module labels, copy sources and obstacle probabilities use `rng.choice`. This reproduces the Section 6.3 checkpoints exactly. Start and goal are `rng.sample(range(81), 2)`, converted with `divmod`.
- **Blocked and cost draws.** Blocked-cell draws are made in row-major order over all non-endpoint cells. Cost draws follow, in row-major order over open non-start cells: one `rng.random()` per cell, with `u < 0.6 → 1`, `u < 0.9 → 2`, otherwise `4`.
- **Beam duplicates.** When a cell is generated more than once within a layer, the entry with the smallest ranking key is kept, which in effect means the lower g-cost. The insertion counter advances for every generated successor.
- **Attempt-start event order** follows Section 10.3 literally: authorization events (`provider_bound`/`executor_instantiated`/`successor_set_bound`), then `task_matched`, then `attempt_started`.
- **Protocol variations** (Section 3.6 allows equivalent signatures):
  - `initialize` returns `PluginInit`, which bundles the state with zero-balance principal requests, so plugins never create accounts themselves.
  - An optional `attempt_started` hook records IB's `instantiated → running` executor transition.
- **Discharge events.** IA and IB emit an additional declared diagnostic event, `obligations_discharged`, at settlement. IC's equivalent is its required `joint_duty_resolved` event.
- **IC details.**
  - `joint_duty_resolved` always names the `performer_id`. Its `discharger_id` is the executor on success and `null` on failure, because a failed duty has no discharger.
  - Member lists are sorted lexicographically, so `FS###` sorts before `P###`.
  - The summary adds derived counts used by the acceptance rows: `fission_successor_count`, `joint_duties_resolved` and `copied_successor_payments`.
- **Event `principal_id` convention:**
  - requester for posting, reservation, matching and verification events;
  - capacity owner for attempt, capacity, binding and resolution events;
  - source for `principal_fissioned`;
  - successor for `successor_commitment_inherited`;
  - payee for `credit_transferred`.
- **Runner replay check.** The runner re-executes the witness in memory and confirms the rendered bytes match (`replay: byte-identical`). Primary outputs are also byte-identical across Python 3.10, 3.11, 3.12 and 3.13.

## Facts about the primary scenario worth knowing

- **Checkpoints.** Seed checkpoints match Section 6.3 exactly. Reference routes span 8–19 moves, and 179 candidate grids were drawn for the 64 tasks.
- **No failures in the primary run.** Every planning module, including bounded beam search, finds a route on all 64 primary grids. All three runs therefore have 64 successes and 0 failures, and IC has 0 failed joint duties. The failure path is covered by tests:
  - a fixture on which width-4 beam fails although a route exists;
  - full IA/IB/IC runs in which beam is forced to fail explicitly, checking releases, failed joint duties, requester balances, conservation and both profiles.
- **IC lineages.**
  - 12 founders were copied at least once, and 4 founders have singleton lineages (`P002`, `P005`, `P008`, `P014`).
  - 4 lineages have three members: `P000`, `P001`, `P010` and `P015`.
  - 56 of the 64 joint duties are bound to multi-member successor sets.
- **Paired traces.** Both profiles select task `T016` on copied capacity `C016`, a copy of `P013`. The slot, timing, expansions and path are identical in all three institutions.
  - In IA, the new principal `CP000` is bound, executes and is paid.
  - In IB, the temporary executor `X-T016-C016` runs under `P013`'s license, and `P013` is paid.
  - In IC, `P013` fissions into successor `FS000`, which inherits the standing commitment. `{FS000, P013}` are bound as co-obligors on `JD-T016`. `FS000` executes and receives the single payment, and the duty is discharged for both members.

## Interpretation limits

A passing run establishes only that these institutional locations are implementable and nonvacuously viable under this one declared finite scenario.

It does not establish:

- historical novelty or uniqueness;
- general viability;
- empirical realism;
- equilibrium, efficiency or incentive properties;
- robustness to other seeds or parameters;
- welfare dominance or an ordering among IA, IB and IC;
- that copying eliminates scarcity.

IC's fission is exogenous and fixed at initialization; it is not a model of endogenous reproduction or of account partition. Compute remains rival by design.

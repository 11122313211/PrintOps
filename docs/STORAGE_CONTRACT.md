# Order storage contract

PrintOps keeps one order per session in storage contract `1.0`: `orderId == sessionId`.
An order may still contain multiple independently identified `items`. This is a
compatibility constraint, not a claim that a session-to-many-orders relationship
is already implemented. The public canonical order view uses schema `2.0`; the
Agent's internal `schemaVersion` is the existing state migration counter.

## Configuration and extension

SQLite remains the default and uses only Python's standard library:

```sh
PRINTOPS_STORE=sqlite PRINTOPS_MEMORY_PATH=data/agent.sqlite3 python3 server.py
```

`PRINTOPS_STORE=memory` selects a process-local volatile backend. It creates no
database file, and loses all orders when the process exits. Independent HTTP or
MCP processes do not share this backend. Use SQLite when they need shared state.
`GET /api/storage` exposes `mode`, `persistent`, `contractVersion`, and
`oneOrderPerSession`, without credentials or filesystem paths. Storage mode is
fixed at process startup; switching a running backend through HTTP is intentionally
unsupported, because that would create another order history.

Custom backends register trusted Python code before constructing `Memory`:

```python
from storage_factory import register_store
from agent import Memory

register_store("custom", lambda path=None: EnterpriseStore())
memory = Memory(mode="custom")
```

`EnterpriseStore` implements the runtime-checkable `OrderStore` protocol in
`storage_contract.py`. Configuration never dynamically imports a user-supplied
module path. Unknown modes fail explicitly. Built-in names cannot be replaced.
Dependency injection is also supported: `Memory(store=my_store)`.

## Record and concurrency semantics

```python
{
    "orderId": "opaque-id",
    "sessionId": "opaque-id",
    "state": {"order": {}, "messages": [], "quoteRequests": [], "revision": 0},
    "revision": 0,
    "status": "draft",
    "createdAt": "UTC ISO-8601",
    "updatedAt": "UTC ISO-8601",
}
```

The protocol has `create_order`, `load_order`, `save_order`, `list_orders`,
`append_event`, `load_events`, `delete_order`, `export_order`, and `metadata`.
Create rejects an existing identifier, including an existing revision `0`.
Update requires `expected_revision`. A stale token raises `StoreConflictError`
and commits nothing. Repeating an identical snapshot does not advance the
revision or create another event. A changed snapshot advances the revision.
Returned records are detached copies.

`save_order` accepts optional `events` and `newMessages` alongside the record.
Those deltas, the snapshot, quote projections and automatic audit entries commit
atomically. A supplied invalid event rolls back the entire operation. Legacy
callers without `newMessages` use a rolling-window overlap comparison; Agent
passes an explicit message delta, so identical repeated utterances are not lost.

`Memory.load/save/save_if_revision/fresh_state` remain available. `Memory.save`
is a compatibility path for fixtures and legacy imports. All Agent saves now
use CAS, including ordinary HTTP conversations; they do not rely on one
process's session mutex. `save_if_revision` normalizes the state once before
committing and returns the committed revision. Existing MCP patch IDs and
idempotency receipts retain their behavior.

SQLite uses WAL, a 5-second busy timeout and `BEGIN IMMEDIATE` for read/compare/write
transactions. Concurrent processes reading the same revision cannot both commit.
An HTTP mutation may also supply `expectedRevision`; stale browser state receives
HTTP `409` / `REVISION_CONFLICT`. A successful response carries the new revision.

The business `status` is distinct from `workflowStage`. Supported states are
`draft`, `collecting`, `ready_for_review`, `confirmed`, `handoff_ready`,
`quote_draft`, `quote_received`, `cancelled`, and `archived`. Quote requests retain
their own separate lifecycle. `delete_order` performs archival, not physical
deletion. Archived records are read-only and retain their history and export.

## SQLite layout and migration

| Table | Purpose |
| --- | --- |
| `orders` | Current order JSON, runtime projection, revision, status and timestamps |
| `order_events` | Append-only state changes with before/after values, actor and revision |
| `messages` | Full ordered message history with durable IDs and timestamps |
| `quote_requests` | Independently addressable current request projections |
| `audit_log` | Confirmation, handoff, quote/status changes, corruption and export actions |
| `sessions` | Transactionally updated legacy compatibility mirror |
| `legacy_session_backups` | Original pre-migration session JSON, preserved verbatim |
| `storage_migrations` | Applied storage schema versions |

`orders` plus the message/quote projections are the backend authority. The
`sessions` mirror is for existing local tools and is written in the same
transaction; it is not another browser-editable source. New integrations must
use the store or API, rather than updating either SQL representation directly.

Opening an existing database creates the new tables and migrates missing orders
within a transaction. Only the snapshot is normalized; the original session JSON
is kept verbatim. Reopening the database does not duplicate orders, messages or
migration events. Invalid legacy JSON is left for the existing quarantine path.
Corruption is backed up under the database directory's `corrupted/` folder with
the current projections before reset. A failed backup does not discard the
original. Append-only events and audit entries survive a projection reset.

The Agent still receives its latest 80 messages and latest 40 quote requests for
bounded working state. The dedicated tables retain older messages/quotes; export
returns all of them. Existing pre-migration windows cannot reconstruct messages
that older releases had already discarded.

## HTTP history API

All endpoints use the existing local access-token guard.

| Method and path | Input | Result |
| --- | --- | --- |
| `GET /api/orders` | `limit` (1–100), `offset`, `includeArchived=true`, optional `status` | `{orders,total,limit,offset,storage}` |
| `GET /api/orders/{id}` | Existing opaque order ID | Read-only UI snapshot with `orderId`, `status`, `revision`, `history`, `canonicalOrder` |
| `POST /api/orders/{id}/archive` | `{expectedRevision}` | Archived order summary with new revision |
| `POST /api/orders/{id}/purge` | `{expectedRevision,confirm:true}`; order must already be archived | `{orderId,deleted:true}`; permanently removes the order snapshot, projections, messages, events, audit rows and legacy mirror |
| `GET /api/orders/{id}/export` | Existing order ID | `{schemaVersion,exportedAt,orderId,sessionId,revision,status,state,canonicalOrder,events,messages,quoteRequests,auditLog}` |
| `POST /api/orders/import-local-history` | `{orders:[legacyRecord,...]}`, at most 100 | `{imported:[id],skipped:[id],errors:[{index,message}],orders:[summary]}` |
| `GET /api/storage` | None | Public backend metadata |

List summaries contain `orderId`, `sessionId`, `revision`, `status`, `createdAt`,
`updatedAt`, `title`, and the compatibility `order` projection. Loading an unknown
order returns `404` rather than creating an empty record. History reads never
execute recommendation tools, append a run or advance the revision. Export
records an audit action without changing the production revision.

The browser migration endpoint accepts the old `{id,order,stage,orderGenerated,
updatedAt}` record shape. Existing backend IDs always win. Only absent IDs are
imported, and concurrent duplicate imports are reported as skipped. Imported
orders are unconfirmed: client `orderGenerated` or `confirmation` flags cannot
become approval. The full original record is retained in a migration event.
The frontend removes its legacy history key only after every record is either
imported or skipped successfully. It then uses backend history exclusively and
retains only the current session ID and UI preferences in localStorage.

## Validation

```sh
python3 -m unittest tests.test_storage tests.test_storage_api tests.test_persistence
python3 -m unittest tests.test_patch_bridge tests.test_runtime_hardening tests.test_http_handler
```

New tests use explicitly synthetic fixtures. They cover both backend contracts,
transaction rollback, repeat messages beyond the context window, migration
idempotency and verbatim backups, cross-connection CAS, archive/export, local
history migration, API revision checks and stale Agent writers. They are not
real customer orders and do not establish a real-corpus extraction accuracy.

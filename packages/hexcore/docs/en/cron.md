# Scheduled tasks (dynamic cron)

`DynamicScheduler` reads its configuration from a **repository**, not from a file. You can enable,
disable or reschedule a job **without restarting anything**.

```python
import hexcore.cqrs as cqrs
```

---

## The complete wiring

```python
import hexcore.cqrs as cqrs

CRON_JOBS = [
    cqrs.cron_job(clean_old_records_task, "*/5 * * * *", payload={"days_retention": 30}),
    cqrs.cron_job(close_books, "0 3 * * *"),
]

await cqrs.create_cron_tables()        # or an Alembic migration
await cqrs.seed_cron_jobs(CRON_JOBS)   # idempotent, and does NOT overwrite database edits

scheduler = cqrs.DynamicScheduler(
    repository=cqrs.SqlAlchemyCronJobRepository(),
    enqueuer=enqueuer,
    lock_provider=lock_provider,
)
```

Then hand it to the runner, which runs it as one more loop:

```python
await cqrs.run_procrastinate_worker(procrastinate_app, scheduler=scheduler)
```

If your cron lives in SQL you do not have to write anything: HexCore ships the table, the
repository and the seed. For another source — Mongo, Redis, a YAML file — implement
`cqrs.ICronJobRepository`: `get_active_jobs()` and `update_last_run(job_id, run_time)`.

---

## `cron_job()`

```python
cqrs.cron_job(
    task,                      # the coroutine decorated with @background_task
    "0 6 1 * *",               # five-field cron expression
    *,
    job_id=None,               # default: derived from task_name
    payload=None,              # the task's kwargs
    queue=None,                # default: the decorator's
    is_active=True,
    description=None,          # default: the first line of the docstring
)
```

The `task_name` comes from `__cqrs_task_name__` and **is not written by hand**. Writing it by hand
is how you end up with a cron enqueuing a task that has since been renamed, and the failure shows
up in the worker, far from where the mistake was made.

### The description

Every job carries a `description` that travels to the table. The scheduler **does not use it**:
it is what an admin panel shows the operator so they know **what a cron does** before disabling
it. With only a `task_name` and a cron expression you cannot tell turning off something harmless
from stopping invoicing.

By default it comes from the first line of the docstring, which is usually exactly that:

```python
@cqrs.background_task(queue="billing")
async def issue_invoices() -> None:
    """Issues this month's invoices and emails them."""
    ...

cqrs.cron_job(issue_invoices, "0 6 1 * *")
# description == "Issues this month's invoices and emails them."

cqrs.cron_job(close_books, "0 3 * * *", description="Closes the day's books.")
```

> If your table predates that column, the migration is a nullable `add_column` —
> `create_cron_tables()` carries it written out in its docstring. A custom model without it does
> not break: the seed inserts whatever the table accepts.

---

## The table

`CronJobModel` (table `hexcore_cron_jobs`) has:

| Column | Type | What it is |
| :-- | :-- | :-- |
| `job_id` | `str` (PK) | Stable identifier |
| `task_name` | `str` | The task to enqueue |
| `cron_expression` | `str` | Five fields |
| `queue` | `str` | Target queue |
| `payload` | `dict` | Kwargs |
| `is_active` | `bool` | The switch the operator edits |
| `last_run_at` | `datetime \| None` | Written by the scheduler |
| `description` | `str \| None` | For the panel |

If you need the table in another schema or with your own columns, `CronJobModelMixin` composes
with your `Base`, and both the repository and the seed accept `model=`.

⚠️ That table is declared by the framework, so Alembic's `env.py` has to call
`ensure_framework_models_loaded()`. Without that line it stays outside `Base.metadata` and the
next `--autogenerate` emits an `op.drop_table` for it.

---

## How it decides whether to run

It does not compare against the current minute. It looks for **any occurrence between
`last_run_at` and now**, and that changes three things:

1. **A minute skipped by tick drift does not lose the run.** Starting at 03:00:30 still fires the
   03:00 job.
2. **`update_last_run` genuinely deduplicates**, so a `tick_interval_seconds < 60` does not
   duplicate within the same process.
3. **`catch_up_window_seconds` (1 hour by default) bounds the catch-up**: a scheduler that was
   down for a week does not fire every missed occurrence at once.

In 2.x it compared against the current minute: with `tick=60s` the accumulated drift skipped a
whole minute and the job did not run; with `tick<60s` it ran twice.

```python
cqrs.DynamicScheduler(
    repository=repo,
    enqueuer=enqueuer,
    lock_provider=lock,
    tick_interval_seconds=30,
    catch_up_window_seconds=3600,
)
```

⚠️ Across **replicas** you need a lock. The scheduler emits a `RuntimeWarning` if it detects a
sub-minute tick with no `lock_provider`: without one, two replicas enqueue the same job.

---

## Distributed locks

```python
import hexcore.cqrs as cqrs

lock_provider = cqrs.RedisLockProvider(redis_client)
```

Or on Postgres, if you would rather not stand up Redis:

```python
lock_provider = cqrs.PostgresLockProvider(asyncpg_pool)
await lock_provider.setup()      # creates the table and index, and purges what expired
```

The Postgres provider **purges itself**: on `setup()` and every 100 acquisitions
(`purge_every`), with a one-hour grace period (`purge_grace_seconds`). In 2.x it never purged, and
that was ~10,000 rows per day, forever, in the main database.

### When the lock does not answer

There are two possible responses and both are bad in different ways, so the decision is yours and
explicit:

```python
cqrs.RedisLockProvider(client, on_error="skip")    # default: do not run; the cron stalls
cqrs.RedisLockProvider(client, on_error="raise")   # propagate, so the supervisor sees it
```

In 2.x the providers returned `False` on *any* error, so a Redis outage switched off the entire
cron with a log line indistinguishable from the normal case. Today the two cases are
distinguishable in the logs:

| Situation | Level |
| :-- | :-- |
| "I could not decide" (the backend failed) | `critical` |
| "Another replica holds the lock" (the normal case) | `debug` |

To write your own provider, implement `ILockProvider`: `acquire_lock(key, ttl_seconds)` and
`release_lock(key)`.

---

## Operating the cron without a restart

The SQL repository exposes what an admin panel needs:

```python
repo = cqrs.SqlAlchemyCronJobRepository()

await repo.get_all_jobs()                    # including the disabled ones
await repo.set_active("issue_invoices", False)
```

`seed_cron_jobs` is idempotent and **does not overwrite database edits**: it inserts what is
missing and leaves alone what is already there. A seed that overwrote would revert, on every
deploy, the job an operator disabled at three in the morning.

---

## Next

→ **[Testing](./testing.md)**.

<img align="right" width="220" alt="pg_smartvac mascot" src="images/pg_smartvac_mascot_clean.png" />

# pg_smartvac

`pg_smartvac` is an intelligent, workload-aware PostgreSQL maintenance utility that independently manages VACUUM, ANALYZE, and FREEZE operations based on table statistics, maintenance history, transaction ID age, and database activity.

Unlike traditional maintenance scripts that rely primarily on fixed schedules, `pg_smartvac` dynamically determines which tables need maintenance and defers execution when database activity is too high.

PostgreSQL 19 introduces its own weighted scoring system to improve built-in autovacuum prioritization. `pg_smartvac` complements these native capabilities by providing DBAs with an additional layer of control over maintenance scheduling, prioritization, and execution.

(c) 2026 SQLEXEC LLC

<div style="clear: right;"></div>


## Requirements
Requires psycopg2 for PG database access.

## History

The first version of this program was created in October 2026, based on my original `pg_vacuum.py` utility, which is not being updated anymore.

## Overview

`pg_smartvac` is designed to complement PostgreSQL autovacuum by identifying tables that need additional maintenance. It evaluates table statistics and maintenance history to determine when VACUUM, ANALYZE, or FREEZE operations should be performed.  The defaults for the parameters if not specified are fairly aggressive.  Consider providing those parameters with different values to decrease or increase the thresholds.

Maintenance decisions are based on configurable thresholds, with a seven-day safety net to ensure that inactive tables are not overlooked.

The program monitors database activity and waits for an acceptable workload window before beginning maintenance.

It runs on Windows and Linux and logs run information to the **pg_smartvac.log** file in the same directory from which this utility is invoked.

Here are the parameters:

| Parameter | Description |
|---|---|
| `-H --host` | Database host or socket directory (default: localhost) |
| `-d --dbname` | Database name (required) |
| `-U --dbuser --user` | Database user (default: postgres) |
| `-p --dbport --port` | Database port (default: 5432) |
| `--vcpus` | Number of vCPUs allocated to the database server (required) |
| `-m --schema` | Restrict maintenance to a specific schema (default: all user schemas) |
| `--vacuum-ratio` | Dead-tuple percentage that triggers VACUUM (default: 2) |
| `--analyze-ratio` | Dead-tuple percentage that triggers ANALYZE (default: 3) |
| `--analyze-mods-ratio` | Percentage of live tuples modified that triggers ANALYZE (default: 2) |
| `--max-table-size-gb` | Maximum table size in GB eligible for maintenance (default: 100) |
| `--max-tables` | Maximum number of tables to maintain per run (default: unlimited; excludes FREEZE) |
| `--max-days` | Maximum number of days before automatic VACUUM/ANALYZE is triggered per run (default: 7) |
| `--vacuum-freeze-table-age-pct` | Percentage of `vacuum_freeze_table_age` at which a table FREEZE is triggered (default: 80) |
| `--max-freeze-tables` | Maximum number of tables to FREEZE per run (default: 20) |
| `-k --disablepageskipping` | Disable visibility-map page skipping during VACUUM |
| `--dryrun` | Display planned maintenance SQL without executing it |
| `-v --verbose` | Enable detailed logging |
| `--version` | Display program version |

## Why pg_smartvac?

PostgreSQL autovacuum is essential for maintaining database health, but DBAs may want additional control over when and how maintenance is performed. `pg_smartvac` complements PostgreSQL's native maintenance capabilities by providing an independent, workload-aware maintenance strategy. Key benefits include:

- **Workload-aware scheduling:** Monitors database activity and defers maintenance until workload conditions are acceptable.
- **Independent maintenance policies:** Uses configurable, percentage-based thresholds and maintenance history to determine when tables need attention.
- **Centralized control:** Manages VACUUM, ANALYZE, and FREEZE through a single utility with configurable limits and priorities.
- **Proactive maintenance:** Identifies tables that may benefit from maintenance before PostgreSQL's normal autovacuum thresholds are reached.
- **Operational visibility:** Logs maintenance decisions and execution details, giving DBAs insight into what was processed and why.
- **TOAST-aware maintenance:** Evaluates TOAST dead tuples independently of parent-table dead-tuple ratios, allowing VACUUM to be triggered when TOAST needs maintenance even if the parent table does not meet its normal threshold.

`pg_smartvac` does not replace or disable PostgreSQL autovacuum. It provides an additional layer of control, allowing DBAs to complement native maintenance with their own scheduling policies and operational preferences.


## Maintenance Thresholds: PostgreSQL GUCs vs. pg_smartvac Ratios

PostgreSQL's built-in autovacuum uses tuple-count thresholds to decide when a table becomes eligible for VACUUM or ANALYZE. These decisions are controlled by configuration parameters (GUCs), including:

- `autovacuum_vacuum_threshold`
- `autovacuum_vacuum_scale_factor`
- `autovacuum_vacuum_max_threshold` (PostgreSQL 18 and later)
- `autovacuum_analyze_threshold`
- `autovacuum_analyze_scale_factor`

For VACUUM, PostgreSQL calculates the trigger threshold using the effective settings for the table:

```text
autovacuum_vacuum_threshold
    + (autovacuum_vacuum_scale_factor * reltuples)
```

the threshold and scale factor.

In PostgreSQL 18 and later, `autovacuum_vacuum_max_threshold` places an upper limit on that calculated threshold. A value of `-1` disables the cap.

For ANALYZE, PostgreSQL calculates the trigger threshold using the effective settings for the table:

```text
autovacuum_analyze_threshold
    + (autovacuum_analyze_scale_factor * reltuples)
```

The effective settings are taken from the table's storage parameters when explicitly configured; otherwise, PostgreSQL uses the corresponding global GUC values. This applies to both the threshold and scale factor.

Autovacuum compares these calculated thresholds with the table's accumulated dead tuples (for VACUUM) or modifications since the last ANALYZE (for ANALYZE). The scale factors make the thresholds grow with table size. Consequently, a large table can accumulate a substantial number of changed or dead tuples before reaching its autovacuum threshold.

### TOAST-Aware VACUUM Decisions:
- TOAST relations have their own live and dead tuple statistics.
- Their VACUUM threshold is calculated using their own effective autovacuum threshold and scale-factor settings.
- Table-specific TOAST storage parameters take precedence over server defaults.
- The PostgreSQL 18+ maximum vacuum threshold is respected when supported.
- When TOAST dead tuples reach the calculated threshold, the parent table qualifies for VACUUM, regardless of its own dead-tuple ratio.
- VACUUM is issued against the parent table, not the TOAST relation directly.

### TOAST FSM Diagnostics:
- FSM free-space information is collected only in verbose mode.
- It is optional and depends on pg_freespacemap being installed and accessible.
- If unavailable, TOAST tuple statistics still work.
- FSM free space is diagnostic only and never triggers VACUUM.


### Why pg_smartvac Uses Ratios

`pg_smartvac` takes a different approach. Instead of reproducing PostgreSQL's tuple-count threshold calculations, it evaluates the current condition of each table using percentage-based ratios.

The dead-tuple ratio used for VACUUM and ANALYZE is:

```text
dead_tuples / (live_tuples + dead_tuples) * 100
```

The modifications ratio used for ANALYZE is:

```text
modifications_since_analyze / live_tuples * 100
```

The default thresholds are:

| Parameter | Default | Purpose |
|---|---:|---|
| `--vacuum-ratio` | 2% | Trigger VACUUM when the dead-tuple ratio reaches this percentage |
| `--analyze-ratio` | 3% | Trigger ANALYZE when the dead-tuple ratio reaches this percentage |
| `--analyze-mods-ratio` | 2% | Trigger ANALYZE when modifications since the last ANALYZE reach this percentage of live tuples |

For example, consider a table with 10 million live tuples and 500,000 dead tuples. Its dead-tuple ratio is approximately 4.76%. That exceeds both the default 2% VACUUM threshold and the default 3% ANALYZE threshold.

This percentage-based approach has several advantages:

- **Consistent relative thresholds:** A 2% dead-tuple ratio represents the same relative table condition whether the table contains 100,000 rows or 100 million rows.
- **More predictable behavior on large tables:** A global scale factor can allow a very large table to accumulate a substantial absolute number of dead tuples before autovacuum reaches its trigger threshold. A ratio gives `pg_smartvac` a direct percentage target.
- **Independent tuning:** VACUUM and ANALYZE have separate ratio settings, allowing VACUUM to be triggered more aggressively than ANALYZE.
- **No need to change autovacuum settings:** `pg_smartvac` applies its own maintenance policy without changing PostgreSQL's GUCs or requiring per-table autovacuum storage parameter overrides.

These mechanisms are complementary, not interchangeable. `pg_smartvac` does not change or disable autovacuum. PostgreSQL's autovacuum continues to operate using its own settings, while `pg_smartvac` provides an additional, independently controlled maintenance mechanism.

The FREEZE policy operates independently of these ratio-based thresholds. It monitors each table's transaction ID age and triggers FREEZE when that age reaches the configured percentage of PostgreSQL's `vacuum_freeze_table_age`. See the Automatic FREEZE Policy section below for details.

In addition to its ratio checks, `pg_smartvac` uses `--max-days` as a time-based safety net. A table can qualify for maintenance because its dead-tuple ratio, modifications ratio, or elapsed time since the previous maintenance operation has reached the configured threshold.

**The goal is proactive, predictable maintenance based on each table's observed condition, rather than relying exclusively on PostgreSQL's built-in tuple-count thresholds.**

**This ratio-based approach allows pg_smartvac to trigger maintenance significantly earlier than PostgreSQL's default autovacuum thresholds, particularly for large tables, helping prevent dead-tuple accumulation and table bloat before they become substantial.**
<br/>
<br/>

## Automatic FREEZE Policy

In addition to its normal VACUUM and ANALYZE decisions, `pg_smartvac` independently monitors the transaction ID age of each table to identify tables approaching PostgreSQL's anti-wraparound vacuum threshold.

By default, a table becomes eligible for FREEZE when its transaction ID age reaches 80% of the current PostgreSQL `vacuum_freeze_table_age` setting. This percentage can be adjusted using `--vacuum-freeze-table-age-pct`.

When triggered, `pg_smartvac` executes:

```sql
VACUUM (FREEZE, ANALYZE) schema.table;
```

FREEZE operations take priority over ordinary VACUUM and ANALYZE operations. Tables with the highest transaction ID age are processed first.

FREEZE operations have their own independent per-run limit, controlled by `--max-freeze-tables` (default: 20). This limit is separate from `--max-tables`, which applies only to ordinary VACUUM and ANALYZE operations.

The existing `--max-table-size-gb` setting applies to FREEZE as well, excluding tables larger than the configured limit from maintenance.

This policy provides an additional safeguard against transaction ID wraparound by proactively freezing tables before PostgreSQL reaches its more aggressive anti-wraparound maintenance threshold.
<br/>
<br/>

## Cron Setup Example
This example runs this tool 4 times per day at the :15 and :45 minute marks. That gives runs at:
- 6:15 AM
- 12:45 PM
- 6:15 PM
- 10:45 PM
  
15 6,18 * * * /usr/bin/python3 /path/to/pg_smartvac.py [arguments] >> /var/log/pg_smartvac_cron.log 2>&1<br/>
45 12,22 * * * /usr/bin/python3 /path/to/pg_smartvac.py [arguments] >> /var/log/pg_smartvac_cron.log 2>&1
<br/>
<br/>

## Invocation

General syntax:

Minimum options using defaults:<br/>
`python pg_smartvac.py -H <dbhost>  -d <dbname> -U <dbuser> -p <dbport> --vcpus <digit> [other options]`

The default settings are normally fine.  Also, don't forget to set the PGPASSWORD environment variable before executing this utility.

The following examples illustrate common ways to invoke the program.

<br/><br/>
**Basic dry run**

Connect to a PostgreSQL database and display the proposed maintenance operations without executing them.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 --dryrun`

Same as before but show rationale for why tables qualified:

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 --dryrun -v`

<br/><br/>
**Restrict maintenance to a schema**

Only consider tables in the specified schema.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 -m public --vcpus 4`

<br/><br/>
**Use custom maintenance thresholds**

Trigger VACUUM when dead tuples reach 1%, ANALYZE when dead tuples reach 1%, and ANALYZE when modifications reach 1% of live tuples.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 --vacuum-ratio 1 --analyze-ratio 1 --analyze-mods-ratio 1`

<br/><br/>
**Limit the number of tables**

Process a maximum of 20 tables in a single run.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 --max-tables 20`

<br/><br/>
**Disable visibility-map page skipping**

Force VACUUM to scan pages that would normally be skipped based on the visibility map.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 -k`

<br/><br/>


## Sample Console and log file screenshots
<img width="1302" height="77" alt="image" src="https://github.com/user-attachments/assets/7e8f7582-658d-488c-867c-d779b8771725" />
<br/>
<br/>
<img width="1083" height="150" alt="image" src="https://github.com/user-attachments/assets/e5d1f67e-32ac-4b3d-a4bf-d08367e944b2" />

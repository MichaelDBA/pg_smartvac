# pg_smartvac

This Python program intelligently manages PostgreSQL VACUUM, ANALYZE, and FREEZE operations based on table statistics, maintenance history, transaction ID age, and database workload.

Unlike traditional maintenance scripts that rely primarily on fixed schedules, `pg_smartvac` dynamically determines which tables need maintenance and defers execution when database activity is too high.

(c) 2026 SQLEXEC LLC
<br/>

## History

The first version of this program was created in October 2026, based on the original `pg_vacuum.py` utility.

## Overview

`pg_smartvac` is designed to complement PostgreSQL autovacuum by identifying tables that need additional maintenance. It evaluates table statistics and maintenance history to determine when VACUUM, ANALYZE, or FREEZE operations should be performed.

Maintenance decisions are based on configurable thresholds, with a seven-day safety net to ensure that inactive tables are not overlooked.

The program monitors database activity and waits for an acceptable workload window before beginning maintenance.

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
| `--analyze-mods-ratio` | Percentage of live tuples modified that triggers ANALYZE (default: 1) |
| `--max-table-size-gb` | Maximum table size in GB eligible for maintenance (default: 100) |
| `--max-tables` | Maximum number of tables to maintain per run (default: unlimited) |
| `-k --disablepageskipping` | Disable visibility-map page skipping during VACUUM |
| `--dryrun` | Display planned maintenance SQL without executing it |
| `-v --verbose` | Enable detailed logging |
| `--version` | Display program version |

## Invocation

General syntax:

`python pg_smartvac.py [options]`

The following examples illustrate common ways to invoke the program.

**Basic dry run**

Connect to a PostgreSQL database and display the proposed maintenance operations without executing them.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 --dryrun`

**Restrict maintenance to a schema**

Only consider tables in the specified schema.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 -m public --vcpus 4 --dryrun`

**Use custom maintenance thresholds**

Trigger VACUUM when dead tuples reach 1%, ANALYZE when dead tuples reach 2%, and ANALYZE when modifications reach 2% of live tuples.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 --vacuum-ratio 1 --analyze-ratio 2 --analyze-mods-ratio 2 --dryrun`

**Limit the number of tables**

Process a maximum of three tables in a single run.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 --max-tables 3 --dryrun`

**Disable visibility-map page skipping**

Force VACUUM to scan pages that would normally be skipped based on the visibility map.

`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 -k --dryrun`

**Enable verbose output**

Display detailed table metrics and the reasons maintenance was selected.

<img width="1759" height="44" alt="image" src="https://github.com/user-attachments/assets/b6c55f8b-463c-4710-9e30-d640991f4ee1" />
<img width="1083" height="101" alt="image" src="https://github.com/user-attachments/assets/e5d1f67e-32ac-4b3d-a4bf-d08367e944b2" />





`python pg_smartvac.py -H localhost -d testing -U postgres -p 5432 --vcpus 4 -v --dryrun`

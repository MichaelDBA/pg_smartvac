# pg_smartvac

This Python (v3.9) program intelligently manages PostgreSQL VACUUM, ANALYZE, and FREEZE operations based on table statistics, maintenance history, transaction ID age, and database workload.

Unlike traditional maintenance scripts that rely primarily on fixed schedules, `pg_smartvac` dynamically determines which tables need maintenance and defers execution when database activity is too high.

(c) 2026 SQLEXEC LLC
<br/>

## History

The first version of this program was created in October 2026, based on the original `pg_vacuum.py` utility, which is not being updated anymore.

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
| `--max-tables` | Maximum number of tables to maintain per run (default: unlimited) |
| `--max-days` | Maximum number of days before automatic VACUUM/ANALYZE is triggered per run (default: 7) |
| `-k --disablepageskipping` | Disable visibility-map page skipping during VACUUM |
| `--dryrun` | Display planned maintenance SQL without executing it |
| `-v --verbose` | Enable detailed logging |
| `--version` | Display program version |

## Cron Setup Example
This example runs this tool 4 times per day at the :15 and :45 minute marks. That gives GoApron runs at:
- 6:15 AM
- 12:45 PM
- 6:15 PM
- 10:45 PM
  
15 6,18 * * * /usr/bin/python3 /path/to/pg_smartvac.py [arguments] >> /var/log/pg_smartvac_cron.log 2>&1<br/>
45 12,22 * * * /usr/bin/python3 /path/to/pg_smartvac.py [arguments] >> /var/log/pg_smartvac_cron.log 2>&1

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

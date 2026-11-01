#!/usr/bin/env python3
##################################################################################################
# pg_smartvac.py
#
# author: Michael Vitale, michaeldba@sqlexec.com
#
# Description: An intelligent, workload-aware PostgreSQL maintenance manager that dynamically determines when to VACUUM, ANALYZE, and FREEZE tables.
#              It won't execute during busy times.
#
# Date Created : October 26, 2026    Original Coding (v 1.0)
# Tested on windows and linux using python 3.9 and PostgreSQL v18
# 
# Requires: py -m pip install psycopg2-binary
#
# Notes:
#   1. Change top shebang line to account for python, python or python2  
#   2. By default, system schemas are not included when schemaname is ommitted from command line.
#
# call example with all parameters:
# python pg_smartvac .py -H localhost -d mydb -U myuser -p 5432 -m public --vcpus 8 --analyze-ratio 1 --analyze-mods-ratio 1 --max-tables 1 --max-table-size-gb 200 --dryrun -v
# python pg_smartvac.py -H ec2-100-58-59-41.compute-1.amazonaws.com -d daauosh1p2e58j -U uephptee5r4t4f -p 5432 -m public  --vcpus 8 --analyze-ratio 1 --analyze-mods-ratio 1 --max-tables 1 --max-table-size-gb 200 --dryrun -v
# python pg_smartvac.py -H michaelv2 -d testing -U postgres -p 5418 -m public  --vcpus 2 --vacuum-ratio 1 --analyze-ratio 1 --analyze-mods-ratio 2  --max-tables 1 --max-table-size-gb 200 -k --dryrun -v
#  where "-k" means disable page skipping, default is off
#
# Date Modified:
# 2026-10-26 Original coding based on pg_vacuum.py
# 2026-11-01 Added more functionality to mimic pg_vacuum.py
#
#
##################################################################################################
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

if sys.platform == "win32":
    import msvcrt
elif sys.platform.startswith("linux"):
    import fcntl
else:
    raise RuntimeError(f"Unsupported operating system: {sys.platform}")

import psycopg2
from psycopg2 import sql

VERSION = "1.0"
VERSION_DATE = "2026-11-01"
DEFAULT_VAC_RATIO = 2
DEFAULT_ANALYZE_RATIO = 3

# Automatic load-management policy.
LOAD_MULTIPLIER = 2.5
DEFAULT_RETRY_SECONDS = 30
DEFAULT_MAX_WAIT_MINUTES = 10

# Log file is created in the current working directory.
SQL_LOG_FILE = "pg_smartvac.log"

# Duplicate-instance protection.
INSTANCE_LOCK_HANDLE = None


def acquire_instance_lock() -> bool:
    global INSTANCE_LOCK_HANDLE

    username = getpass.getuser()
    safe_username = "".join(c if c.isalnum() or c in "-_." else "_" for c in username)
    lock_path = os.path.join(tempfile.gettempdir(), f"pg_smartvac_{safe_username}.lock")

    try:
        lock_file = open(lock_path, "a+b")

        if sys.platform == "win32":
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        INSTANCE_LOCK_HANDLE = lock_file
        return True

    except OSError as exc:
        if "lock_file" in locals():
            lock_file.close()

        print(f"Cannot acquire pg_smartvac instance lock: {exc}", file=sys.stderr)
        print("Another instance may already be running.", file=sys.stderr)
        return False


def release_instance_lock() -> None:
    global INSTANCE_LOCK_HANDLE

    if INSTANCE_LOCK_HANDLE is None:
        return

    try:
        if sys.platform == "win32":
            INSTANCE_LOCK_HANDLE.seek(0)
            msvcrt.locking(INSTANCE_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(INSTANCE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)

    finally:
        INSTANCE_LOCK_HANDLE.close()
        INSTANCE_LOCK_HANDLE = None


def log_event(message: str) -> None:
    """Append one timestamped, single-line run or SQL event to the log."""
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    one_line_message = str(message).rstrip()

    with open(SQL_LOG_FILE, "a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {one_line_message}\n")


def format_log_message(label: str, message: str) -> str:
    return f"{label:<8} - {message}"


def log_info(label: str, message: str, *args) -> None:
    formatted_message = message % args if args else message
    logging.info(format_log_message(label, formatted_message))


def log_summary(summary: dict, dryrun: bool) -> None:
    if dryrun:
        line = (
            f"evaluated={summary['evaluated']} * "
            f"maintained={summary['maintained']} * "
            f"skipped={summary['skipped']} * "
            f"failed={summary['failed']} * "
            f"planned_tables={summary['planned_tables']} * "
            f"VACUUM={summary['planned_vacuum']} * "
            f"ANALYZE={summary['planned_analyze']} * "
            f"FREEZE={summary['planned_freeze']}"
        )
    else:
        line = (
            f"evaluated={summary['evaluated']} * "
            f"maintained={summary['maintained']} * "
            f"skipped={summary['skipped']} * "
            f"failed={summary['failed']} * "
            f"VACUUM={summary['vacuum']} * "
            f"ANALYZE={summary['analyze']} * "
            f"FREEZE={summary['freeze']}"
        )

    message = format_log_message("SUMMARY", line)
    log_event(f"INFO: {message}")
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} INFO: {message}", flush=True)


def log_sql_action(statement: str) -> None:
    """Append one timestamped line for each maintenance SQL command."""
    log_event(statement)


@dataclass
class TableCandidate:
    schema: str
    table: str
    total_bytes: int
    estimated_rows: int
    live_tuples: int
    dead_tuples: int
    modifications_since_analyze: int
    last_vacuum: Optional[datetime]
    last_autovacuum: Optional[datetime]
    last_analyze: Optional[datetime]
    last_autoanalyze: Optional[datetime]
    xid_age: int

    @property
    def qualified_name(self) -> str:
        escaped_schema = self.schema.replace('"', '""')
        escaped_table = self.table.replace('"', '""')
        return f'"{escaped_schema}"."{escaped_table}"'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone, load-aware PostgreSQL VACUUM / ANALYZE manager.")

    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}",)

    # Essential connection settings.
    parser.add_argument("-H", "--host", default="", help="database host or socket directory",)
    parser.add_argument("-d", "--dbname", required=True, help="database name",)
    parser.add_argument("-U", "--dbuser", "--user", dest="dbuser", default="postgres", help="database user (default: postgres)",)
    parser.add_argument("-p", "--dbport", "--port", dest="dbport", type=int, default=5432, help="database port (default: 5432)",)

    # Load detection.
    parser.add_argument("--vcpus", type=float, required=True, help="number of vCPUs allocated to the database server",)

    # schema restriction
    parser.add_argument("-m", "--schema", default="", help="only process this schema (default: all user schemas)",)

    # Operational controls.    
    parser.add_argument("--vacuum-ratio", type=int, default=DEFAULT_VAC_RATIO, help="dead-tuple percentage that triggers VACUUM (default: 2)",)
    parser.add_argument("--analyze-ratio", type=int, default=DEFAULT_ANALYZE_RATIO, help="dead-tuple percentage that triggers ANALYZE (default: 3)",)    
    parser.add_argument("--analyze-mods-ratio", type=int, default=1, help="percentage of live tuples modified that triggers ANALYZE (default: 1)",)    
    parser.add_argument("--max-table-size-gb", type=int, default=100, help="maximum table size in GB eligible for maintenance (default: 100)",)
    parser.add_argument("--max-tables", type=int, default=None, help="maximum number of tables to maintain per run (default: unlimited)",)
    parser.add_argument("-k", "--disablepageskipping", action="store_true", help="disable visibility-map page skipping during VACUUM (default: off)",)

    parser.add_argument("--dryrun", action="store_true", help="show actions without executing them",)
    parser.add_argument("-v", "--verbose", action="store_true", help="enable detailed logging",)

    return parser.parse_args()


def configure_logging(args: argparse.Namespace) -> None:
    level = logging.INFO if args.verbose else logging.WARNING

    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S",)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(SQL_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler], force=True,)
    logging.addLevelName(logging.WARNING, "WARN")


def connect(args: argparse.Namespace):
    kwargs = {"dbname": args.dbname, "user": args.dbuser, "host": args.host, "port": args.dbport, "application_name": "pg_smartvac", "connect_timeout": 10,}
    connection = psycopg2.connect(**kwargs)

    # VACUUM cannot run inside a transaction block.
    connection.autocommit = True
    return connection


def active_session_count(connection) -> int:
    # Intentionally includes the pg_smartvac connection.
    # The threshold is generalized, so one extra session is acceptable.
    with connection.cursor() as cursor:
        cursor.execute("""SELECT count(*) FROM pg_stat_activity WHERE state IN ('active', 'idle in transaction')""")
        return int(cursor.fetchone()[0])



def get_active_vacuum_tables(connection) -> set[tuple[str, str]]:
    """Return schema/table pairs currently undergoing VACUUM."""
    query = """SELECT n.nspname, c.relname FROM pg_stat_progress_vacuum p JOIN pg_class c ON c.oid = p.relid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE p.datid = (SELECT oid FROM pg_database WHERE datname = current_database())"""

    with connection.cursor() as cursor:
        cursor.execute(query)
        return {(row[0], row[1]) for row in cursor.fetchall()}



def wait_for_load_window(
    connection,
    args: argparse.Namespace,
) -> bool:
    threshold = args.vcpus * LOAD_MULTIPLIER
    deadline = time.monotonic() + DEFAULT_MAX_WAIT_MINUTES * 60

    while True:
        active = active_session_count(connection)

        log_info("LOAD", "vCPUs=%s active_sessions=%s threshold=%s", args.vcpus, active, threshold)

        # Proceed when sessions are at or below the threshold.
        if active <= threshold:
            return True

        if time.monotonic() >= deadline:
            logging.warning("Deferring maintenance: active sessions (%s) exceed threshold (%s). Exiting this run.", active, threshold,)
            return False

        remaining = max(0, int(deadline - time.monotonic()))
        sleep_for = min(DEFAULT_RETRY_SECONDS, remaining)

        logging.info("Database is over the load threshold; retrying in %s seconds (up to %s minutes total wait).", sleep_for, DEFAULT_MAX_WAIT_MINUTES,)
        time.sleep(sleep_for)


def table_candidates(
    connection,
    args: argparse.Namespace,
) -> list[TableCandidate]:
    query = """
        SELECT
            n.nspname,
            c.relname,
            pg_total_relation_size(c.oid) AS total_bytes,
            GREATEST(c.reltuples, 0)::bigint AS estimated_rows,
            COALESCE(s.n_live_tup, 0)::bigint AS live_tuples,
            COALESCE(s.n_dead_tup, 0)::bigint AS dead_tuples,            
            COALESCE(s.n_mod_since_analyze, 0)::bigint
                AS modifications_since_analyze,
            s.last_vacuum,
            s.last_autovacuum,
            s.last_analyze,
            s.last_autoanalyze,
            age(c.relfrozenxid)::bigint AS xid_age,
            c.relkind,
            c.relispartition
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.relkind IN ('r', 'm', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND c.relpersistence <> 't'
        ORDER BY n.nspname, c.relname
    """



    with connection.cursor() as cursor:
        cursor.execute("SHOW vacuum_freeze_min_age")
        args.vacuum_freeze_min_age = int(cursor.fetchone()[0])
        
        cursor.execute(query)
        rows = cursor.fetchall()

    now = datetime.now(timezone.utc)
    candidates: list[TableCandidate] = []

    for row in rows:
        (schema, table, total_bytes, estimated_rows, live_tuples, dead_tuples, modifications, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze, xid_age, relkind, is_partition,) = row

        if args.schema and schema != args.schema:
            continue

        # Skip partitioned parents and individual partitions for v0.1.
        if relkind == "p" or is_partition:
            continue

        # Skip tables larger than the configured maximum size.
        max_size_bytes = args.max_table_size_gb * 1_000_000_000
        if total_bytes > max_size_bytes:
            warning = (f"Skipping table {schema}.{table}: table size {total_bytes / 1_000_000_000:.2f} GB exceeds the {args.max_table_size_gb} GB limit.")
            logging.warning(warning)
            continue
            
        last_vac = latest_timestamp(last_vacuum, last_autovacuum)
        last_ana = latest_timestamp(last_analyze, last_autoanalyze)

        dead_ratio = (dead_tuples / (live_tuples + dead_tuples) * 100 if (live_tuples + dead_tuples) > 0 else 0)

        vacuum_by_age = is_older_than(last_vac, 7, now)
        vacuum_by_ratio = dead_ratio >= args.vacuum_ratio

        analyze_by_age = is_older_than(last_ana, 7, now)
        analyze_by_ratio = dead_ratio >= args.analyze_ratio
        analyze_mods_ratio = (modifications / live_tuples * 100 if live_tuples > 0 else (100 if modifications > 0 else 0))
        analyze_by_mods = analyze_mods_ratio >= args.analyze_mods_ratio        

        freeze_due = xid_age >= args.vacuum_freeze_min_age

        vacuum_due = last_vac is None or vacuum_by_age or vacuum_by_ratio
        analyze_due = (last_ana is None or analyze_by_age or analyze_by_ratio or analyze_by_mods)        
        
        if vacuum_due or analyze_due or freeze_due:
            candidates.append(
                TableCandidate(
                    schema=schema,
                    table=table,
                    total_bytes=int(total_bytes),
                    estimated_rows=int(estimated_rows),
                    live_tuples=int(live_tuples),
                    dead_tuples=int(dead_tuples),
                    modifications_since_analyze=int(modifications),
                    last_vacuum=last_vacuum,
                    last_autovacuum=last_autovacuum,
                    last_analyze=last_analyze,
                    last_autoanalyze=last_autoanalyze,
                    xid_age=int(xid_age),
                )
            )

    candidates.sort(key=lambda t: earliest_timestamp(t.last_vacuum, t.last_autovacuum,) or datetime.min.replace(tzinfo=timezone.utc))

    return candidates[:9999]


def latest_timestamp(
    a: Optional[datetime],
    b: Optional[datetime],
) -> Optional[datetime]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def earliest_timestamp(
    a: Optional[datetime],
    b: Optional[datetime],
) -> Optional[datetime]:
    values = [value for value in (a, b) if value is not None]

    if not values:
        return None

    normalized = [
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value
        for value in values
    ]

    return min(normalized)


def is_older_than(
    timestamp: Optional[datetime],
    days: int,
    now: datetime,
) -> bool:
    if timestamp is None:
        return True

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return (now - timestamp).total_seconds() >= days * 86400



def determine_actions(
    table: TableCandidate,
    args: argparse.Namespace,
) -> tuple[list[str], list[str]]:
    now = datetime.now(timezone.utc)

    last_vac = latest_timestamp(table.last_vacuum, table.last_autovacuum)
    last_ana = latest_timestamp(table.last_analyze, table.last_autoanalyze)

    dead_ratio = (table.dead_tuples / (table.live_tuples + table.dead_tuples) * 100 if (table.live_tuples + table.dead_tuples) > 0 else 0)

    vacuum_by_age = is_older_than(last_vac, 7, now)
    vacuum_by_ratio = dead_ratio >= args.vacuum_ratio

    analyze_by_age = is_older_than(last_ana, 7, now)
    analyze_by_ratio = dead_ratio >= args.analyze_ratio
    analyze_mods_ratio = (
        table.modifications_since_analyze / table.live_tuples * 100
        if table.live_tuples > 0
        else (100 if table.modifications_since_analyze > 0 else 0)
    )
    
    analyze_by_mods = analyze_mods_ratio >= args.analyze_mods_ratio
    freeze_due = table.xid_age >= args.vacuum_freeze_min_age

    vacuum_due = last_vac is None or vacuum_by_age or vacuum_by_ratio
    analyze_due = (last_ana is None or analyze_by_age or analyze_by_ratio or analyze_by_mods)

    vacuum_reasons = []
    analyze_reasons = []

    if last_vac is None:
        vacuum_reasons.append("no previous VACUUM recorded")
    elif vacuum_by_age:
        vacuum_reasons.append("last VACUUM was at least 7 days ago")

    if vacuum_by_ratio:
        vacuum_reasons.append(f"dead-tuple ratio {dead_ratio:.2f}% >= VACUUM threshold {args.vacuum_ratio}%")

    if last_ana is None:
        analyze_reasons.append("no previous ANALYZE recorded")
    elif analyze_by_age:
        analyze_reasons.append("last ANALYZE was at least 7 days ago")

    if analyze_by_ratio:
        analyze_reasons.append(f"dead-tuple ratio {dead_ratio:.2f}% >= ANALYZE threshold {args.analyze_ratio}%")

    if analyze_by_mods:
        analyze_reasons.append(f"modifications ratio {analyze_mods_ratio:.2f}% >= ANALYZE modifications threshold {args.analyze_mods_ratio}%")

    if freeze_due:
        return ["FREEZE"], ["freeze policy triggered"]

    if vacuum_due and analyze_due:
        return ["VACUUM ANALYZE"], vacuum_reasons + analyze_reasons

    if vacuum_due:
        return ["VACUUM"], vacuum_reasons

    if analyze_due:
        return ["ANALYZE"], analyze_reasons

    return [], []


def perform_action(
    connection,
    table: TableCandidate,
    action: str,
    args: argparse.Namespace,
) -> None:
    if action in ("FREEZE", "VACUUM", "VACUUM ANALYZE"):
        options = []

        if action == "FREEZE":
            options.extend(["FREEZE", "ANALYZE"])
        elif action == "VACUUM ANALYZE":
            options.append("ANALYZE")

        if args.verbose:
            options.append("VERBOSE")

        if args.disablepageskipping:
            options.append("DISABLE_PAGE_SKIPPING")

        if options:
            statement = sql.SQL("VACUUM ({}) {}").format(sql.SQL(", ").join(sql.SQL(option) for option in options), sql.Identifier(table.schema, table.table),)
        else:
            statement = sql.SQL("VACUUM {}").format(sql.Identifier(table.schema, table.table))

    elif action == "ANALYZE":
        statement = sql.SQL("ANALYZE {}").format(sql.Identifier(table.schema, table.table))

    else:
        return

    sql_text = statement.as_string(connection).rstrip(";") + ";"

    # Record proposed SQL without executing it in dry-run mode.
    if args.dryrun:
        log_info("DRY RUN", "%s", sql_text)
        if not args.verbose:
            print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} INFO: {format_log_message('DRY RUN', sql_text)}", flush=True)            
            
        return        
        
    # uncomment 2 lines below for exception handling testing        
    #if table.schema == "public" and table.table == "smartvac_test":
    #    raise RuntimeError("SIMULATED ERROR: Testing maintenance failure handling.")

    sql_text = statement.as_string(connection).rstrip(";") + ";"
    with connection.cursor() as cursor:
        print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} INFO: Executing - {sql_text}", flush=True)
        cursor.execute(statement)

    # Record the command after PostgreSQL executes it successfully.
    logging.info("Executed - %s", sql_text)
    if not args.verbose:
        print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} INFO: Executed - {sql_text}", flush=True)
        

def main() -> int:
    args = parse_args()
    configure_logging(args)

    if args.vcpus <= 0:
        logging.error("--vcpus must be greater than zero.")
        return 2

    if not 0 <= args.vacuum_ratio <= 100:
        logging.error("--vac-ratio must be an integer from 0 to 100.")
        return 2

    if not 0 <= args.analyze_ratio <= 100:
        logging.error("--analyze-ratio must be an integer from 0 to 100.")
        return 2

    if not 0 <= args.analyze_mods_ratio <= 100:
        logging.error("--analyze-mods-ratio must be an integer from 0 to 100.")
        return 2

    if args.max_table_size_gb <= 0:
        logging.error("--max-table-size-gb must be greater than zero.")
        return 2

    if args.max_tables is not None and args.max_tables <= 0:
        logging.error("--max-tables must be greater than zero.")
        return 2

    if not acquire_instance_lock():
        logging.error("Another pg_smartvac instance is already running. Exiting.")
        return 1

    connection = None
    run_status = "FAILED"

    summary = {
        "evaluated": 0,
        "maintained": 0,
        "skipped": 0,
        "failed": 0,
        "vacuum": 0,
        "analyze": 0,
        "freeze": 0,
        "planned_tables": 0,
        "planned_vacuum": 0,
        "planned_analyze": 0,
        "planned_freeze": 0,
    }

    try:
        log_event(f"INFO: {format_log_message('START', f'version={VERSION}  date={VERSION_DATE}  database={args.dbname}  host={args.host or "localhost"}  port={args.dbport}  schema={args.schema or "all-user-schemas"}  dryrun={args.dryrun}  verbose={args.verbose}')}")

        connection = connect(args)
        
        if not wait_for_load_window(connection, args):
            run_status = "DEFERRED_LOAD"
            log_event("DEFERRED: database session count remained above threshold")
            return 2

        candidates = table_candidates(connection, args)
        summary["evaluated"] = len(candidates)

        if not candidates:
            log_event("INFO: NO ACTION    - no tables qualified for maintenance")
            run_status = "COMPLETED_NO_ACTION"
            return 0

        log_info("TABLES", "%s eligible for evaluation.", len(candidates))
        active_vacuums = get_active_vacuum_tables(connection)
        processed_tables = 0
        deferred_tables = 0

        for table in candidates:
            if (table.schema, table.table) in active_vacuums:
                message = f"Skipping {table.schema}.{table.table}: VACUUM already in progress."
                logging.info(message)
                if not args.verbose:
                    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} INFO: {message}", flush=True)
                summary["skipped"] += 1
                continue

            actions, reasons = determine_actions(table, args)
            if not actions:
                summary["skipped"] += 1
                continue

            if args.max_tables is not None and processed_tables >= args.max_tables:
                deferred_tables += 1
                summary["skipped"] += 1
                continue

            processed_tables += 1

            dead_ratio = (table.dead_tuples / (table.live_tuples + table.dead_tuples) * 100 if (table.live_tuples + table.dead_tuples) > 0 else 0)

            table_check = (
                f"{table.schema}.{table.table} "
                f"size={table.total_bytes / 1_000_000_000:.2f} GB "
                f"estimated_rows={table.estimated_rows} "
                f"live_tuples={table.live_tuples} "
                f"dead_tuples={table.dead_tuples} "
                f"dead_ratio={dead_ratio:.2f}% "
                f"modifications={table.modifications_since_analyze} "
                f"xid_age={table.xid_age} "
                f"actions={','.join(actions) if actions else 'NONE'}"
            )

            if args.verbose:
                log_info("METRICS", "%s", table_check)

                for reason in reasons:
                    log_info("REASON", "%s", reason)

            table_failed = False

            for action in actions:
                try:
                    perform_action(connection, table, action, args)

                    if args.dryrun:
                        if action in ("VACUUM", "VACUUM ANALYZE", "FREEZE"):
                            summary["planned_vacuum"] += 1
                        if action in ("ANALYZE", "VACUUM ANALYZE", "FREEZE"):
                            summary["planned_analyze"] += 1
                        if action == "FREEZE":
                            summary["planned_freeze"] += 1
                    else:
                        if action in ("VACUUM", "VACUUM ANALYZE", "FREEZE"):
                            summary["vacuum"] += 1
                        if action in ("ANALYZE", "VACUUM ANALYZE", "FREEZE"):
                            summary["analyze"] += 1
                        if action == "FREEZE":
                            summary["freeze"] += 1

                except Exception as exc:
                    logging.error("Maintenance failed for %s.%s: %s", table.schema, table.table, exc)
                    table_failed = True
                    break
                    
            if table_failed:
                summary["failed"] += 1
            elif args.dryrun:
                summary["planned_tables"] += 1
            else:
                summary["maintained"] += 1

        if deferred_tables:
            message = f"Deferred {deferred_tables} table(s): --max-tables limit of {args.max_tables} reached."
            logging.info(message)
            if not args.verbose:
                print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} INFO: {message}", flush=True)

        if summary["failed"]:
            run_status = "COMPLETED_WITH_ERRORS"
        else:
            run_status = "COMPLETED"

        return 0
        
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        run_status = "INTERRUPTED"
        log_event("INTERRUPTED: run stopped by user")
        return 130

    except psycopg2.Error as exc:
        logging.error("PostgreSQL error: %s", exc)
        log_event(f"PG ERROR:  {exc}")
        return 1

    except Exception as exc:
        logging.exception("Unexpected error.")
        log_event(f"ERROR unexpected: {exc}")
        return 1


    finally:
        try:
            if connection is not None:
                connection.close()
            log_summary(summary, args.dryrun)
            log_event(f"INFO: {format_log_message('END', run_status)}")
        finally:
            release_instance_lock()


if __name__ == "__main__":
    sys.exit(main())

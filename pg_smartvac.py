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
# call example using default parameters in verbose, dryrun mode:
# python pg_smartvac .py -H localhost -d mydb -U myuser -p 5432 -m public --vcpus 8 --dryrun -v
#
# Date Modified:
# 2026-10-26 Original coding based on pg_vacuum.py
# 2026-11-02 Added more functionality to incorporate other pg_vacuum.py features.
# 2026-11-05 Added more parameters to control MINs and MAXs and better FREEZE logic.
# 2026-11-07 Added toast table analysis
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

VERSION = "1.2"
VERSION_DATE = "2026-11-07"

# Defaults if parameters are not specified
DEFAULT_VAC_RATIO = 2
DEFAULT_ANALYZE_RATIO = 3
DEFAULT_ANALYZE_MODS_RATIO = 2
DEFAULT_MAX_DAYS = 7
DEFAULT_MAX_TBL_SIZE_GB = 100
DEFAULT_FREEZE_TBL_AGE_PCT = 80
DEFAULT_FREEZE_MAX_TABLES = 20

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
            f"VACUUM/ANALYZE={summary['planned_vacuum_analyze']} * "
            f"VACUUM={summary['planned_vacuum_only']} * "
            f"ANALYZE={summary['planned_analyze_only']} * "
            f"FREEZE={summary['planned_freeze']}"
        )
    else:
        line = (
            f"evaluated={summary['evaluated']} * "
            f"maintained={summary['maintained']} * "
            f"skipped={summary['skipped']} * "
            f"failed={summary['failed']} * "
            f"VACUUM/ANALYZE={summary['vacuum_analyze']} * "
            f"VACUUM={summary['vacuum_only']} * "
            f"ANALYZE={summary['analyze_only']} * "
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
    toast_oid: Optional[int]
    toast_pages: int
    toast_live_tuples: int
    toast_dead_tuples: int
    toast_vacuum_threshold: Optional[int]
    toast_fsm_free_bytes: Optional[int] = None

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
    parser.add_argument("--vacuum-ratio", type=int, default=DEFAULT_VAC_RATIO, help=f"dead-tuple percentage that triggers VACUUM (default: {DEFAULT_VAC_RATIO})",)
    parser.add_argument("--analyze-ratio", type=int, default=DEFAULT_ANALYZE_RATIO, help=f"dead-tuple percentage that triggers ANALYZE (default: {DEFAULT_ANALYZE_RATIO})",)
    parser.add_argument("--analyze-mods-ratio", type=int, default=DEFAULT_ANALYZE_MODS_RATIO, help=f"percentage of live tuples modified that triggers ANALYZE (default: {DEFAULT_ANALYZE_MODS_RATIO})",)
    parser.add_argument("--max-table-size-gb", type=int, default=DEFAULT_MAX_TBL_SIZE_GB, help=f"maximum table size in GB eligible for maintenance (default: {DEFAULT_MAX_TBL_SIZE_GB})",)    
    parser.add_argument("--max-tables", type=int, default=None, help="maximum number of ordinary VACUUM/ANALYZE tables per run (default: unlimited; excludes FREEZE)",)
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS, help=f"elapsed days before VACUUM or ANALYZE is triggered (default: {DEFAULT_MAX_DAYS})",)
    parser.add_argument("--vacuum-freeze-table-age-pct", type=int, default=DEFAULT_FREEZE_TBL_AGE_PCT, help=f"percentage of vacuum_freeze_table_age that triggers FREEZE (default: {DEFAULT_FREEZE_TBL_AGE_PCT})",)
    parser.add_argument("--max-freeze-tables", type=int, default=DEFAULT_FREEZE_MAX_TABLES, help=f"maximum number of tables to FREEZE per run (default: {DEFAULT_FREEZE_MAX_TABLES})",)    
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
            COALESCE(s.n_mod_since_analyze, 0)::bigint AS modifications_since_analyze,
            s.last_vacuum,
            s.last_autovacuum,
            s.last_analyze,
            s.last_autoanalyze,
            age(c.relfrozenxid)::bigint AS xid_age,
            tc.oid AS toast_oid,
            CASE WHEN tc.oid IS NOT NULL THEN (pg_relation_size(tc.oid) / current_setting('block_size')::bigint) ELSE 0 END AS toast_pages,
            COALESCE(ts.n_live_tup, 0)::bigint AS toast_live_tuples,
            COALESCE(ts.n_dead_tup, 0)::bigint AS toast_dead_tuples,
            CASE
                WHEN tc.oid IS NOT NULL THEN
                    CEIL(LEAST(
                        COALESCE(NULLIF(current_setting('autovacuum_vacuum_max_threshold', TRUE), '')::float8, 'Infinity'::float8),
                        COALESCE(toast_opts.vacuum_threshold, current_setting('autovacuum_vacuum_threshold')::float8)
                        + COALESCE(toast_opts.vacuum_scale_factor, current_setting('autovacuum_vacuum_scale_factor')::float8)
                        * COALESCE(ts.n_live_tup, 0)
                    ))::bigint
            END AS toast_vacuum_threshold,
            c.relkind,
            c.relispartition
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        LEFT JOIN pg_class tc ON tc.oid = c.reltoastrelid AND c.reltoastrelid <> 0
        LEFT JOIN pg_stat_all_tables ts ON ts.relid = tc.oid
        LEFT JOIN LATERAL (
            SELECT
                MAX(option_value::float8) FILTER (WHERE option_name = 'autovacuum_vacuum_threshold') AS vacuum_threshold,
                MAX(option_value::float8) FILTER (WHERE option_name = 'autovacuum_vacuum_scale_factor') AS vacuum_scale_factor
            FROM pg_options_to_table(tc.reloptions)
        ) toast_opts ON TRUE
        WHERE c.relkind IN ('r', 'm', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND c.relpersistence <> 't'
        ORDER BY n.nspname, c.relname
    """


    with connection.cursor() as cursor:
        cursor.execute("SHOW vacuum_freeze_table_age")
        args.vacuum_freeze_table_age = int(cursor.fetchone()[0])
        args.freeze_threshold = int(args.vacuum_freeze_table_age * args.vacuum_freeze_table_age_pct / 100)

        cursor.execute("SELECT to_regprocedure('pg_freespace(regclass,bigint)') IS NOT NULL")
        args.toast_fsm_available = bool(cursor.fetchone()[0])

        cursor.execute(query)
        rows = cursor.fetchall()
        
    now = datetime.now(timezone.utc)
    candidates: list[TableCandidate] = []

    for row in rows:
        (schema, table, total_bytes, estimated_rows, live_tuples, dead_tuples, modifications, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze, xid_age, toast_oid, toast_pages, toast_live_tuples, toast_dead_tuples, toast_vacuum_threshold, relkind, is_partition,) = row

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

        vacuum_by_age = is_older_than(last_vac, args.max_days, now)
        vacuum_by_ratio = dead_ratio >= args.vacuum_ratio
        toast_vacuum_due = toast_vacuum_threshold is not None and toast_dead_tuples >= toast_vacuum_threshold
        analyze_by_age = is_older_than(last_ana, args.max_days, now)

        analyze_by_ratio = dead_ratio >= args.analyze_ratio
        analyze_mods_ratio = (modifications / live_tuples * 100 if live_tuples > 0 else (100 if modifications > 0 else 0))
        analyze_by_mods = analyze_mods_ratio >= args.analyze_mods_ratio

        freeze_due = xid_age >= args.freeze_threshold

        vacuum_due = last_vac is None or vacuum_by_age or vacuum_by_ratio or toast_vacuum_due
        analyze_due = (last_ana is None or analyze_by_age or analyze_by_ratio or analyze_by_mods)
        
        if vacuum_due or analyze_due or freeze_due:
            candidates.append(TableCandidate(schema=schema, table=table, total_bytes=int(total_bytes), estimated_rows=int(estimated_rows), live_tuples=int(live_tuples), dead_tuples=int(dead_tuples),
                              modifications_since_analyze=int(modifications), last_vacuum=last_vacuum, last_autovacuum=last_autovacuum, last_analyze=last_analyze, last_autoanalyze=last_autoanalyze, xid_age=int(xid_age),
                              toast_oid=int(toast_oid) if toast_oid is not None else None, toast_pages=int(toast_pages), toast_live_tuples=int(toast_live_tuples), toast_dead_tuples=int(toast_dead_tuples), 
                              toast_vacuum_threshold=int(toast_vacuum_threshold) if toast_vacuum_threshold is not None else None,))

    # Process FREEZE candidates first, prioritizing the oldest XID age.
    # Preserve the existing oldest-VACUUM-first ordering for other candidates.
    candidates.sort(
                    key=lambda t: (0 if t.xid_age >= args.freeze_threshold else 1,
                    -t.xid_age if t.xid_age >= args.freeze_threshold else 0,
                    earliest_timestamp(t.last_vacuum, t.last_autovacuum,) or datetime.min.replace(tzinfo=timezone.utc),)
                   )    

    # Collect TOAST FSM free space only for verbose diagnostics.
    # FSM availability does not affect maintenance decisions.
    if args.verbose and args.toast_fsm_available:
        try:
            with connection.cursor() as cursor:
                for candidate in candidates:
                    if candidate.toast_oid is None or candidate.toast_pages <= 0:
                        continue

                    cursor.execute(
                        "SELECT COALESCE(SUM(pg_freespace(%s::regclass, blkno)), 0)::bigint FROM generate_series(0, %s::bigint - 1) AS blkno",
                        (candidate.toast_oid, candidate.toast_pages),
                    )
                    candidate.toast_fsm_free_bytes = int(cursor.fetchone()[0])

        except psycopg2.Error as exc:
            logging.warning("TOAST FSM diagnostic unavailable: %s", exc)
            args.toast_fsm_available = False

            for candidate in candidates:
                candidate.toast_fsm_free_bytes = None

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

    vacuum_by_age = is_older_than(last_vac, args.max_days, now)
    vacuum_by_ratio = dead_ratio >= args.vacuum_ratio
    toast_vacuum_due = table.toast_vacuum_threshold is not None and table.toast_dead_tuples >= table.toast_vacuum_threshold
    analyze_by_age = is_older_than(last_ana, args.max_days, now)

    analyze_by_ratio = dead_ratio >= args.analyze_ratio
    analyze_mods_ratio = (table.modifications_since_analyze / table.live_tuples * 100 if table.live_tuples > 0 else (100 if table.modifications_since_analyze > 0 else 0))

    analyze_by_mods = analyze_mods_ratio >= args.analyze_mods_ratio
    freeze_due = table.xid_age >= args.freeze_threshold

    vacuum_due = last_vac is None or vacuum_by_age or vacuum_by_ratio or toast_vacuum_due
    analyze_due = (last_ana is None or analyze_by_age or analyze_by_ratio or analyze_by_mods)
    
    vacuum_reasons = []
    analyze_reasons = []

    if last_vac is None:
        vacuum_reasons.append("no previous VACUUM recorded")
    elif vacuum_by_age:
        vacuum_reasons.append(f"last VACUUM was at least {args.max_days} days ago")        

    if vacuum_by_ratio:
        vacuum_reasons.append(f"dead-tuple ratio {dead_ratio:.2f}% >= VACUUM threshold {args.vacuum_ratio}%")

    if toast_vacuum_due:
        vacuum_reasons.append(f"TOAST dead tuples {table.toast_dead_tuples:,} >= TOAST VACUUM threshold {table.toast_vacuum_threshold:,}") 

    if last_ana is None:
        analyze_reasons.append("no previous ANALYZE recorded")
    elif analyze_by_age:
        analyze_reasons.append(f"last ANALYZE was at least {args.max_days} days ago")        

    if analyze_by_ratio:
        analyze_reasons.append(f"dead-tuple ratio {dead_ratio:.2f}% >= ANALYZE threshold {args.analyze_ratio}%")

    if analyze_by_mods:
        analyze_reasons.append(f"modifications ratio {analyze_mods_ratio:.2f}% >= ANALYZE modifications threshold {args.analyze_mods_ratio}%")

    if freeze_due:
        return ["FREEZE"], [f"table XID age {table.xid_age:,} >= FREEZE threshold {args.freeze_threshold:,} ({args.vacuum_freeze_table_age_pct}% of vacuum_freeze_table_age {args.vacuum_freeze_table_age:,})"]

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
        logging.error("--vacuum-ratio must be an integer from 0 to 100.")
        return 2

    if not 0 <= args.analyze_ratio <= 100:
        logging.error("--analyze-ratio must be an integer from 0 to 100.")
        return 2

    if not 0 <= args.analyze_mods_ratio <= 100:
        logging.error("--analyze-mods-ratio must be an integer from 0 to 100.")
        return 2

    if not 1 <= args.vacuum_freeze_table_age_pct <= 100:
        logging.error("--vacuum-freeze-table-age-pct must be an integer from 1 to 100.")
        return 2

    if args.max_table_size_gb <= 0:
        logging.error("--max-table-size-gb must be greater than zero.")
        return 2

    if args.max_tables is not None and args.max_tables <= 0:
        logging.error("--max-tables must be greater than zero.")
        return 2

    if args.max_freeze_tables <= 0:
        logging.error("--max-freeze-tables must be greater than zero.")
        return 2

    if args.max_days <= 0:
        logging.error("--max-days must be greater than zero.")
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
        "vacuum_analyze": 0,
        "vacuum_only": 0,
        "analyze_only": 0,
        "freeze": 0,
        "planned_tables": 0,
        "planned_vacuum_analyze": 0,
        "planned_vacuum_only": 0,
        "planned_analyze_only": 0,
        "planned_freeze": 0,
    }
    
    try:
        start_message = (
            f"version={VERSION}  date={VERSION_DATE}  database={args.dbname}  "
            f"host={args.host or 'localhost'}  port={args.dbport}  "
            f"schema={args.schema or 'all-user-schemas'}  "
            f"dryrun={args.dryrun}  verbose={args.verbose}"
        )
        log_event(f"INFO: {format_log_message('START', start_message)}")        

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
        freeze_tables = 0
        deferred_tables = 0
        deferred_freeze_tables = 0        

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

            is_freeze = actions == ["FREEZE"]

            if is_freeze:
                if freeze_tables >= args.max_freeze_tables:
                    deferred_freeze_tables += 1
                    summary["skipped"] += 1
                    continue
                # Count each selected FREEZE table as an attempt, including failures.
                freeze_tables += 1
            else:
                if args.max_tables is not None and processed_tables >= args.max_tables:
                    deferred_tables += 1
                    summary["skipped"] += 1
                    continue
                processed_tables += 1            

            dead_ratio = (table.dead_tuples / (table.live_tuples + table.dead_tuples) * 100 if (table.live_tuples + table.dead_tuples) > 0 else 0)

            if table.toast_oid is None:
                toast_metrics = "toast=none"
            else:
                toast_fsm = (
                    str(table.toast_fsm_free_bytes)
                    if table.toast_fsm_free_bytes is not None
                    else ("unavailable" if not args.toast_fsm_available else "not_scanned")
                )
                toast_metrics = (
                    f"toast_live={table.toast_live_tuples} "
                    f"toast_dead={table.toast_dead_tuples} "
                    f"toast_threshold={table.toast_vacuum_threshold} "
                    f"toast_fsm_free_bytes={toast_fsm}"
                )

            table_check = (
                f"{table.schema}.{table.table} "
                f"size={table.total_bytes / 1_000_000_000:.2f} GB "
                f"estimated_rows={table.estimated_rows} "
                f"live_tuples={table.live_tuples} "
                f"dead_tuples={table.dead_tuples} "
                f"dead_ratio={dead_ratio:.2f}% "
                f"{toast_metrics} "
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
                        if action == "VACUUM ANALYZE":
                            summary["planned_vacuum_analyze"] += 1
                        elif action == "VACUUM":
                            summary["planned_vacuum_only"] += 1
                        elif action == "ANALYZE":
                            summary["planned_analyze_only"] += 1
                        elif action == "FREEZE":
                            summary["planned_freeze"] += 1
                    else:
                        if action == "VACUUM ANALYZE":
                            summary["vacuum_analyze"] += 1
                        elif action == "VACUUM":
                            summary["vacuum_only"] += 1
                        elif action == "ANALYZE":
                            summary["analyze_only"] += 1
                        elif action == "FREEZE":
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

        if deferred_freeze_tables:
            message = f"Deferred {deferred_freeze_tables} table(s): --max-freeze-tables limit of {args.max_freeze_tables} reached."
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

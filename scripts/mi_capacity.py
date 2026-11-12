"""Capacity context for a load into Azure SQL Managed Instance.

Why this module exists
----------------------
The run collector (collect_run_metrics.py) measures what a load did to the
sink: waits, file I/O, and log usage. On a Managed Instance, a DBA also needs
to know how close that load came to the limits Azure places on the instance,
and whether something other than the load was using the instance at the same
time. This module adds that context to the benchmark record.

It follows the same idea as sql/monitoring/azure_sql_mi_file_diagnostic_v2-3.sql
but is built for one recorded test, not for live troubleshooting:

* It reads nothing while the load runs. Every value comes from the 'before'
  and 'after' snapshots the collector already takes.
* The limits come from Microsoft's published resource-limits page. The page
  and the date it was read are recorded with every result, so a DBA can tell
  which numbers a record was judged against.
* Waits are the proof. A limit was reached when the engine says it made work
  wait for it. Usage compared with a published limit is context: it is an
  average over the window, so it can hide short bursts.
* It gives evidence, not instructions. It never suggests changes to files,
  storage, or service tier; that is the DBA's call.
* A value that cannot be worked out is recorded as None with the reason. It
  is never estimated.

Only Python's standard library is used.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

# SERVERPROPERTY('EngineEdition') value for Azure SQL Managed Instance.
ENGINE_EDITION_MANAGED_INSTANCE = 8

# ---------------------------------------------------------------------------
# Published limits
# ---------------------------------------------------------------------------
# Update these values, LIMITS_VERSION, and LIMITS_RETRIEVED together when
# Microsoft changes the page. Records keep the version they were judged with.

LIMITS_SOURCE = "https://learn.microsoft.com/azure/azure-sql/managed-instance/resource-limits"
LIMITS_RETRIEVED = "2026-11-12"
LIMITS_VERSION = "mi-resource-limits-2026-11-12"

# Classic General Purpose: every database file has its own IOPS and throughput
# limit, set by the file's size. (largest size in GiB, IOPS, MiB/s)
GP_CLASSIC_FILE_LIMITS: tuple[tuple[float, int, int], ...] = (
    (129, 500, 100),
    (513, 2300, 150),
    (1025, 5000, 200),
    (2049, 7500, 250),
    (4097, 7500, 250),
    (8192, 7500, 250),
)

# Next-gen General Purpose: the instance has one IOPS pool. It is 3 IOPS per GB
# of reserved storage (at least 300), plus any extra IOPS bought, up to a cap
# set by the vCore count. (vCores, max IOPS, max MB/s); 56 to 128 vCores share
# the last row.
NEXT_GEN_VCORE_LIMITS: tuple[tuple[int, int, int], ...] = (
    (4, 6400, 145), (6, 9600, 217), (8, 12800, 290), (10, 16000, 367),
    (12, 19200, 445), (16, 25600, 600), (20, 32000, 665), (24, 38400, 730),
    (32, 51200, 865), (40, 64000, 1152), (48, 76800, 1152), (56, 80000, 1200),
)
NEXT_GEN_IOPS_PER_RESERVED_GB = 3
NEXT_GEN_MINIMUM_IOPS = 300
NEXT_GEN_IOPS_PER_MB_PER_SECOND = 30   # throughput (MB/s) = IOPS / 30
NEXT_GEN_MINIMUM_MB_PER_SECOND = 75

# Business Critical: 4,000 IOPS per vCore, at most 320,000. Throughput is not limited.
BC_IOPS_PER_VCORE = 4000
BC_MAXIMUM_IOPS = 320000

# Instance log write limit: MiB/s per vCore, and the maximum for the instance
# by hardware. Classic General Purpose has one maximum for all hardware.
LOG_RATE_LIMITS: dict[str, tuple[float, dict[str, float]]] = {
    "classic": (4.5, {"standard": 120.0, "premium": 120.0}),
    "next-gen": (4.5, {"standard": 120.0, "premium": 192.0}),
    "business-critical": (16.0, {"standard": 160.0, "premium": 192.0}),
}

# Thresholds used to call a value "near a limit" and an I/O "slow". They are
# the same as in azure_sql_mi_file_diagnostic_v2-3.sql so that the two tools
# agree.
NEAR_LIMIT_PERCENT = 70.0
SLOW_IO_MS = 15.0

# Another database doing more than this share of the instance's I/O during the
# window means the test did not have the instance to itself.
OTHER_WORKLOAD_SHARE_PERCENT = 25.0

TEMPDB_DATABASE_ID = 2
BYTES_PER_GIB = 1073741824
BYTES_PER_MIB = 1048576
BYTES_PER_MB = 1000000

GP_STORAGE_CHOICES: tuple[str, ...] = ("classic", "next-gen")

# ---------------------------------------------------------------------------
# Snapshot queries (read-only)
# ---------------------------------------------------------------------------

# The newest row describes the instance as it is now: tier, hardware, vCores,
# and storage. Needs VIEW SERVER STATE, the same as the wait statistics.
SQL_MI_INSTANCE = """
SELECT TOP (1)
    sku,
    hardware_generation,
    virtual_core_count,
    reserved_storage_mb,
    storage_space_used_mb
FROM sys.server_resource_stats
ORDER BY end_time DESC;
"""

# Cumulative I/O counters and the allocated size of every file of every
# database on the instance. One query; no database is switched into.
SQL_MI_INSTANCE_FILES = """
SELECT
    vfs.database_id,
    DB_NAME(vfs.database_id)        AS database_name,
    vfs.file_id,
    mf.name                         AS logical_file_name,
    mf.type_desc                    AS file_type,
    CAST(mf.size AS bigint) * 8192  AS size_bytes,
    vfs.num_of_reads,
    vfs.num_of_writes,
    vfs.num_of_bytes_read,
    vfs.num_of_bytes_written,
    vfs.io_stall_read_ms,
    vfs.io_stall_write_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
INNER JOIN sys.master_files AS mf
    ON mf.database_id = vfs.database_id
   AND mf.file_id = vfs.file_id;
"""

FILE_COUNTERS: tuple[str, ...] = (
    "num_of_reads", "num_of_writes", "num_of_bytes_read", "num_of_bytes_written",
    "io_stall_read_ms", "io_stall_write_ms",
)

Fetch = Callable[[Any, str], list]


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}".strip()


def capture_mi_snapshot(connection: Any, fetch: Fetch) -> dict[str, Any]:
    """Read the instance description and every file's I/O counters.

    Each part is read on its own. If one cannot be read (most often because
    the sign-in lacks VIEW SERVER STATE), it is recorded as not collected with
    the reason, and the rest of the snapshot is unaffected.
    """
    snapshot: dict[str, Any] = {"instance": None, "files": None, "not_collected": {}}
    try:
        rows = fetch(connection, SQL_MI_INSTANCE)
        if rows:
            row = rows[0]
            snapshot["instance"] = {
                "sku": None if row.get("sku") is None else str(row["sku"]),
                "hardware_generation": None if row.get("hardware_generation") is None else str(row["hardware_generation"]),
                "vcores": None if row.get("virtual_core_count") is None else int(row["virtual_core_count"]),
                "reserved_storage_mb": None if row.get("reserved_storage_mb") is None else int(row["reserved_storage_mb"]),
                "storage_used_mb": None if row.get("storage_space_used_mb") is None else float(row["storage_space_used_mb"]),
            }
        else:
            snapshot["not_collected"]["instance"] = "sys.server_resource_stats returned no rows."
    except Exception as exc:  # permissions or platform; never fail the test for context
        snapshot["not_collected"]["instance"] = _reason(exc)
    try:
        snapshot["files"] = [
            {
                "database_id": int(row["database_id"]),
                "database_name": None if row.get("database_name") is None else str(row["database_name"]),
                "file_id": int(row["file_id"]),
                "logical_file_name": str(row["logical_file_name"]),
                "file_type": str(row["file_type"]),
                "size_bytes": None if row.get("size_bytes") is None else int(row["size_bytes"]),
                **{name: int(row[name]) for name in FILE_COUNTERS},
            }
            for row in fetch(connection, SQL_MI_INSTANCE_FILES)
        ]
    except Exception as exc:
        snapshot["not_collected"]["files"] = _reason(exc)
    return snapshot


# ---------------------------------------------------------------------------
# Limits for this instance
# ---------------------------------------------------------------------------

def tier_of(sku: str | None) -> str | None:
    """'general-purpose', 'business-critical', or None when the SKU is not recognised."""
    text = (sku or "").lower()
    if "general" in text:
        return "general-purpose"
    if "business" in text:
        return "business-critical"
    return None


def hardware_series(hardware_generation: str | None) -> str | None:
    """'standard', 'premium', or None when the hardware name is not recognised.

    Standard-series hardware reports as Gen5 (Gen6 and Gen7 are the same series
    on newer processors). Premium-series hardware reports as Gen8.
    """
    text = (hardware_generation or "").lower().replace(" ", "")
    if "premium" in text or "gen8" in text:
        return "premium"
    if any(name in text for name in ("gen5", "gen6", "gen7", "standard")):
        return "standard"
    return None


def gp_file_limit(size_bytes: int | None) -> tuple[int | None, int | None]:
    """IOPS and MiB/s limit of one classic General Purpose file of this size."""
    if size_bytes is None:
        return None, None
    size_gib = size_bytes / BYTES_PER_GIB
    for largest_gib, iops, mib_per_second in GP_CLASSIC_FILE_LIMITS:
        if size_gib <= largest_gib:
            return iops, mib_per_second
    return GP_CLASSIC_FILE_LIMITS[-1][1], GP_CLASSIC_FILE_LIMITS[-1][2]


def next_gen_vcore_limit(vcores: int | None) -> tuple[int | None, int | None]:
    """Maximum IOPS and MB/s for a Next-gen General Purpose instance with this many vCores."""
    if not vcores or vcores < NEXT_GEN_VCORE_LIMITS[0][0]:
        return None, None
    chosen = NEXT_GEN_VCORE_LIMITS[0]
    for row in NEXT_GEN_VCORE_LIMITS:
        if vcores >= row[0]:
            chosen = row
    return chosen[1], chosen[2]


def log_rate_limit(storage: str | None, vcores: int | None, series: str | None) -> tuple[float | None, str | None]:
    """Instance log write limit in MiB/s, or None with the reason it is unknown."""
    if storage not in LOG_RATE_LIMITS:
        return None, "The service tier or General Purpose storage type is not known."
    if not vcores:
        return None, "The vCore count is not known."
    per_vcore, maximum = LOG_RATE_LIMITS[storage]
    candidates = {name: min(per_vcore * vcores, cap) for name, cap in maximum.items()}
    if series in candidates:
        return candidates[series], None
    if len(set(candidates.values())) == 1:  # the hardware makes no difference at this size
        return next(iter(candidates.values())), None
    return None, "The hardware series is not recognised, and the limit differs between series at this vCore count."


def instance_limits(instance: dict[str, Any] | None, stated: dict[str, Any]) -> dict[str, Any]:
    """Work out which published limits apply to this instance."""
    limits: dict[str, Any] = {
        "storage": None,
        "iops": None,
        "throughput_mb_per_second": None,
        "log_write_mib_per_second": None,
        "per_file_limits": False,
        "notes": [],
    }
    if not instance:
        limits["notes"].append("The instance description was not collected, so no limits are known.")
        return limits

    tier = tier_of(instance.get("sku"))
    vcores = instance.get("vcores")
    series = hardware_series(instance.get("hardware_generation"))
    if tier == "business-critical":
        storage = "business-critical"
    elif tier == "general-purpose":
        storage = stated.get("gp_storage")
        if storage not in GP_STORAGE_CHOICES:
            storage = None
            limits["notes"].append(
                "General Purpose storage type was not stated (--mi-gp-storage classic or next-gen on 'start'). "
                "Classic and Next-gen General Purpose have different limits, and the type cannot be read "
                "reliably from SQL, so storage limits were not applied."
            )
    else:
        storage = None
        limits["notes"].append(f"Service tier {instance.get('sku')!r} is not recognised.")
    limits["storage"] = storage

    limits["log_write_mib_per_second"], log_note = log_rate_limit(storage, vcores, series)
    if tier == "general-purpose" and storage is None:
        # Both General Purpose types share the per-vCore log rate; when they
        # also give the same maximum at this size, the limit is known anyway.
        both = {log_rate_limit(kind, vcores, series)[0] for kind in GP_STORAGE_CHOICES}
        if len(both) == 1 and None not in both:
            limits["log_write_mib_per_second"], log_note = both.pop(), None
    if log_note:
        limits["notes"].append(f"Instance log write limit unknown: {log_note}")

    if storage == "classic":
        limits["per_file_limits"] = True
        limits["notes"].append(
            "Classic General Purpose has no single instance IOPS limit; each file has its own, set by its size. "
            "The per-database log limit (22-65 MiB/s, depending on log file size) is not published per size and "
            "is not assessed."
        )
    elif storage == "next-gen":
        cap_iops, cap_mb = next_gen_vcore_limit(vcores)
        if cap_iops is None:
            limits["notes"].append("Next-gen General Purpose limits are not published for this vCore count.")
        else:
            reserved_gb = (instance.get("reserved_storage_mb") or 0) / 1024
            included = max(int(reserved_gb * NEXT_GEN_IOPS_PER_RESERVED_GB), NEXT_GEN_MINIMUM_IOPS)
            provisioned = stated.get("iops")
            if provisioned:
                iops = min(int(provisioned), cap_iops)
                if provisioned > cap_iops:
                    limits["notes"].append(f"Stated IOPS {provisioned:,} is above the {cap_iops:,} cap for {vcores} vCores; the cap is used.")
            else:
                iops = min(included, cap_iops)
                limits["notes"].append(
                    f"IOPS limit assumes the included {included:,} IOPS (3 per reserved GB). "
                    "If extra IOPS were bought, state the total with --mi-iops on 'start'."
                )
            limits["iops"] = iops
            limits["throughput_mb_per_second"] = round(
                min(max(iops / NEXT_GEN_IOPS_PER_MB_PER_SECOND, NEXT_GEN_MINIMUM_MB_PER_SECOND), cap_mb), 1
            )
    elif storage == "business-critical" and vcores:
        limits["iops"] = min(BC_IOPS_PER_VCORE * vcores, BC_MAXIMUM_IOPS)
        limits["notes"].append("Business Critical throughput is not limited.")
    return limits


# ---------------------------------------------------------------------------
# Assessment of one test
# ---------------------------------------------------------------------------

def _rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _per_second(value: float, seconds: float | None) -> float | None:
    return None if not seconds else value / seconds


def _percent(used: float | None, limit: float | None) -> float | None:
    return None if used is None or not limit else round(100.0 * used / limit, 1)


def _file_deltas(before: Sequence[dict[str, Any]], after: Sequence[dict[str, Any]],
                 seconds: float | None) -> tuple[list[dict[str, Any]], int]:
    """Per-file activity in the window. Returns the active files and how many were reset."""
    earlier = {(f["database_id"], f["file_id"]): f for f in before}
    files, resets = [], 0
    for a in after:
        b = earlier.get((a["database_id"], a["file_id"]))
        if b is None:
            continue  # created during the test; no starting point
        delta = {name: a[name] - b[name] for name in FILE_COUNTERS}
        if any(value < 0 for value in delta.values()):
            resets += 1
            continue
        operations = delta["num_of_reads"] + delta["num_of_writes"]
        if operations == 0:
            continue
        moved = delta["num_of_bytes_read"] + delta["num_of_bytes_written"]
        files.append({
            "database_id": a["database_id"],
            "database_name": a["database_name"],
            "logical_file_name": a["logical_file_name"],
            "file_type": a["file_type"],
            "size_gib": _rounded(None if a["size_bytes"] is None else a["size_bytes"] / BYTES_PER_GIB, 2),
            "_size_bytes": a["size_bytes"],
            "_operations": operations,
            "_bytes": moved,
            "_log_bytes_written": delta["num_of_bytes_written"] if a["file_type"].upper() == "LOG" else 0,
            "reads": delta["num_of_reads"],
            "writes": delta["num_of_writes"],
            "iops": _rounded(_per_second(operations, seconds)),
            "mib_per_second": _rounded(_per_second(moved / BYTES_PER_MIB, seconds), 2),
            "read_latency_ms": _rounded(delta["io_stall_read_ms"] / delta["num_of_reads"], 2) if delta["num_of_reads"] else None,
            "write_latency_ms": _rounded(delta["io_stall_write_ms"] / delta["num_of_writes"], 2) if delta["num_of_writes"] else None,
        })
    return files, resets


def _slowest(file: dict[str, Any]) -> float:
    return max(file.get("read_latency_ms") or 0.0, file.get("write_latency_ms") or 0.0)


def _instance_changed(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    if not before or not after:
        return False
    keys = ("sku", "hardware_generation", "vcores", "reserved_storage_mb")
    return any(before.get(k) != after.get(k) for k in keys)


def assess(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    window_seconds: float | None,
    waits: dict[str, dict[str, Any]],
    stated: dict[str, Any] | None,
    target_database: str | None,
) -> dict[str, Any]:
    """Compare one test's instance-wide I/O with the instance's published limits.

    ``before``/``after`` are the 'managed_instance' parts of the two snapshots.
    ``waits`` are the wait deltas from compute_sql_deltas. ``stated`` holds what
    the tester said about the instance on 'start' (GP storage type, IOPS).
    """
    stated = stated or {}
    warnings: list[str] = []
    result: dict[str, Any] = {
        "applicable": True,
        "limits_version": LIMITS_VERSION,
        "limits_source": LIMITS_SOURCE,
        "limits_retrieved": LIMITS_RETRIEVED,
        "thresholds": {"near_limit_percent": NEAR_LIMIT_PERCENT, "slow_io_ms": SLOW_IO_MS},
        "stated": {"gp_storage": stated.get("gp_storage"), "iops": stated.get("iops")},
        "not_collected": {},
    }
    if not before or not after:
        result.update({"collected": False, "classification": "NOT_ASSESSED",
                       "evidence": ["Managed Instance context was not captured in both snapshots."],
                       "warnings": warnings})
        return result
    for part in ("instance", "files"):
        reason = (before.get("not_collected") or {}).get(part) or (after.get("not_collected") or {}).get(part)
        if reason:
            result["not_collected"][part] = reason
    result["collected"] = True

    instance = before.get("instance") or after.get("instance")
    result["instance"] = instance
    if _instance_changed(before.get("instance"), after.get("instance")):
        warnings.append(
            "The instance's tier, hardware, vCores, or reserved storage changed during the test. "
            "Limits are judged against the 'before' description."
        )
    limits = instance_limits(instance, stated)
    result["limits"] = limits

    # --- what happened ------------------------------------------------------
    files: list[dict[str, Any]] = []
    if before.get("files") is not None and after.get("files") is not None:
        files, resets = _file_deltas(before["files"], after["files"], window_seconds)
        if resets:
            warnings.append(f"I/O counters of {resets} file(s) were reset during the test; those files are excluded.")

    storage = limits["storage"]
    # In General Purpose, tempdb is on local SSD and outside the storage limits.
    # In Business Critical, it shares the instance's IOPS.
    counted = [f for f in files if storage == "business-critical" or f["database_id"] != TEMPDB_DATABASE_ID]
    total_operations = sum(f["_operations"] for f in files)
    observed = {
        "iops": _rounded(_per_second(sum(f["_operations"] for f in counted), window_seconds)),
        "throughput_mb_per_second": _rounded(_per_second(sum(f["_bytes"] for f in counted) / BYTES_PER_MB, window_seconds), 2),
        "log_write_mib_per_second": _rounded(
            _per_second(sum(f["_log_bytes_written"] for f in files) / BYTES_PER_MIB, window_seconds), 2),
    }
    result["observed"] = observed
    result["utilisation_percent"] = {
        "iops": _percent(observed["iops"], limits["iops"]),
        "throughput": _percent(observed["throughput_mb_per_second"], limits["throughput_mb_per_second"]),
        "log_write": _percent(observed["log_write_mib_per_second"], limits["log_write_mib_per_second"]),
    }

    # --- which databases did the I/O ----------------------------------------
    by_database: dict[str, dict[str, Any]] = {}
    for f in files:
        entry = by_database.setdefault(f["database_name"] or str(f["database_id"]),
                                       {"database_name": f["database_name"], "_operations": 0, "_bytes": 0, "_log": 0})
        entry["_operations"] += f["_operations"]
        entry["_bytes"] += f["_bytes"]
        entry["_log"] += f["_log_bytes_written"]
    databases = []
    for entry in sorted(by_database.values(), key=lambda e: e["_operations"], reverse=True):
        databases.append({
            "database_name": entry["database_name"],
            "is_target": entry["database_name"] == target_database,
            "share_of_io_percent": _percent(entry["_operations"], total_operations),
            "iops": _rounded(_per_second(entry["_operations"], window_seconds)),
            "mib_per_second": _rounded(_per_second(entry["_bytes"] / BYTES_PER_MIB, window_seconds), 2),
            "log_write_mib_per_second": _rounded(_per_second(entry["_log"] / BYTES_PER_MIB, window_seconds), 2),
        })
    result["databases"] = databases
    others = [d for d in databases if not d["is_target"] and (d["share_of_io_percent"] or 0) > OTHER_WORKLOAD_SHARE_PERCENT]
    for other in others:
        warnings.append(
            f"Database {other['database_name']} did {other['share_of_io_percent']}% of the instance's I/O during "
            "the test. The load did not have the instance to itself."
        )

    # --- per-file limits (classic General Purpose) ---------------------------
    for f in files:
        f["iops_limit"] = f["mib_per_second_limit"] = None
        if limits["per_file_limits"] and f["database_id"] != TEMPDB_DATABASE_ID:
            f["iops_limit"], f["mib_per_second_limit"] = gp_file_limit(f["_size_bytes"])
        f["iops_percent"] = _percent(f["iops"], f["iops_limit"])
        f["throughput_percent"] = _percent(f["mib_per_second"], f["mib_per_second_limit"])
    result["files"] = [
        {k: v for k, v in f.items() if not k.startswith("_")}
        for f in sorted(files, key=lambda f: f["_operations"], reverse=True)
    ]

    # --- classification -----------------------------------------------------
    evidence: list[str] = []
    governor_ms = (waits.get("INSTANCE_LOG_RATE_GOVERNOR") or {}).get("wait_time_ms")
    writelog_ms = (waits.get("WRITELOG") or {}).get("wait_time_ms")
    instance_pressure = False
    if governor_ms:
        instance_pressure = True
        evidence.append(f"INSTANCE_LOG_RATE_GOVERNOR: {governor_ms:,} ms of waiting. The instance log write limit was reached.")
    if writelog_ms:
        evidence.append(f"WRITELOG: {writelog_ms:,} ms of waiting for log writes to finish.")

    usage_lines = (
        ("log write rate", observed["log_write_mib_per_second"], limits["log_write_mib_per_second"], "MiB/s", "log_write"),
        ("instance IOPS", observed["iops"], limits["iops"], "IOPS", "iops"),
        ("instance throughput", observed["throughput_mb_per_second"], limits["throughput_mb_per_second"], "MB/s", "throughput"),
    )
    for label, used, limit, unit, key in usage_lines:
        percent = result["utilisation_percent"][key]
        if percent is None:
            continue
        evidence.append(f"Average {label}: {used:,} {unit}, {percent}% of the {limit:,} {unit} limit.")
        if percent >= NEAR_LIMIT_PERCENT:
            instance_pressure = True

    file_pressure = [
        f for f in result["files"]
        if max(f["iops_percent"] or 0, f["throughput_percent"] or 0) >= NEAR_LIMIT_PERCENT and _slowest(f) > SLOW_IO_MS
    ]
    for f in file_pressure:
        evidence.append(
            f"File {f['database_name']}.{f['logical_file_name']} ({f['file_type']}, {f['size_gib']} GiB): "
            f"{f['iops']:,} IOPS ({f['iops_percent']}% of {f['iops_limit']:,}), {f['mib_per_second']} MiB/s "
            f"({f['throughput_percent']}% of {f['mib_per_second_limit']}), slowest latency {_slowest(f)} ms."
        )
    slow = [f for f in result["files"] if _slowest(f) > SLOW_IO_MS and f not in file_pressure]
    for f in slow[:5]:
        evidence.append(
            f"File {f['database_name']}.{f['logical_file_name']} ({f['file_type']}) was slow "
            f"({_slowest(f)} ms) without being near a known limit."
        )

    if instance_pressure and file_pressure:
        classification = "MIXED"
    elif instance_pressure:
        classification = "INSTANCE_LEVEL"
    elif file_pressure:
        classification = "FILE_LEVEL"
    elif slow:
        classification = "NOT_PROVEN"
    elif files:
        # "Healthy" needs the storage limits; without them it cannot be said.
        classification = "HEALTHY" if storage else "NOT_ASSESSED"
    else:
        classification = "NO_ACTIVITY"
    if classification == "NO_ACTIVITY" and result["not_collected"].get("files"):
        classification = "NOT_ASSESSED"
    result["classification"] = classification
    result["evidence"] = evidence
    result["warnings"] = warnings
    return result


CLASSIFICATION_MEANING: dict[str, str] = {
    "INSTANCE_LEVEL": "An instance-wide limit was reached or nearly reached.",
    "FILE_LEVEL": "One or more files were near their own limit and slow, while the instance was not.",
    "MIXED": "Both an instance-wide limit and one or more file limits were under pressure.",
    "NOT_PROVEN": "Some I/O was slow, but nothing was near a known limit. Look elsewhere before blaming storage limits.",
    "HEALTHY": "No limit was near, and I/O was not slow.",
    "NO_ACTIVITY": "No file I/O was recorded in the window.",
    "NOT_ASSESSED": "Not enough was collected, or stated, to judge. See the notes below.",
}

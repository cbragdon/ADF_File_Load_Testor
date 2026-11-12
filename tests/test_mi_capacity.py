"""Tests for scripts/mi_capacity.py.

No database is needed: snapshots are built in memory, and the one capture
test uses a small fake fetch function.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mi_capacity as m

GIB = m.BYTES_PER_GIB
WINDOW = 100.0


def file_row(db_id, db_name, file_id, name, kind, size_gib, reads=0, writes=0, bytes_read=0, bytes_written=0,
             read_stall=0, write_stall=0):
    return {
        "database_id": db_id, "database_name": db_name, "file_id": file_id, "logical_file_name": name,
        "file_type": kind, "size_bytes": int(size_gib * GIB),
        "num_of_reads": reads, "num_of_writes": writes, "num_of_bytes_read": bytes_read,
        "num_of_bytes_written": bytes_written, "io_stall_read_ms": read_stall, "io_stall_write_ms": write_stall,
    }


def instance(sku="General Purpose", hardware="Gen5", vcores=8, reserved_mb=1024 * 1024):
    return {"sku": sku, "hardware_generation": hardware, "vcores": vcores,
            "reserved_storage_mb": reserved_mb, "storage_used_mb": 1000.0}


def snapshots(after_files, before_files=None, inst=None, after_inst=None):
    """'before' with zeroed counters (unless given) and 'after' with the given files."""
    if before_files is None:
        before_files = [dict(f, **{k: 0 for k in m.FILE_COUNTERS}) for f in after_files]
    inst = inst or instance()
    return ({"instance": inst, "files": before_files, "not_collected": {}},
            {"instance": after_inst or inst, "files": after_files, "not_collected": {}})


def waits(governor_ms=0, writelog_ms=0):
    return {"INSTANCE_LOG_RATE_GOVERNOR": {"wait_time_ms": governor_ms}, "WRITELOG": {"wait_time_ms": writelog_ms}}


def quiet_load():
    """The target database writing modestly to a 100 GiB data file and a log file."""
    return [
        file_row(5, "LoadTest", 1, "LoadTest", "ROWS", 100, writes=10000, bytes_written=GIB, write_stall=50000),
        file_row(5, "LoadTest", 2, "LoadTest_log", "LOG", 20, writes=5000, bytes_written=GIB, write_stall=10000),
    ]


def run(after_files, stated=None, governor_ms=0, writelog_ms=0, **kwargs):
    before, after = snapshots(after_files, **kwargs)
    return m.assess(before, after, WINDOW, waits(governor_ms, writelog_ms),
                    stated if stated is not None else {"gp_storage": "classic"}, "LoadTest")


class PublishedLimitTests(unittest.TestCase):
    def test_classic_file_limits_follow_the_size_brackets(self):
        self.assertEqual(m.gp_file_limit(129 * GIB), (500, 100))
        self.assertEqual(m.gp_file_limit(130 * GIB), (2300, 150))
        self.assertEqual(m.gp_file_limit(1025 * GIB), (5000, 200))
        self.assertEqual(m.gp_file_limit(3000 * GIB), (7500, 250))
        self.assertEqual(m.gp_file_limit(None), (None, None))

    def test_next_gen_uses_included_iops_up_to_the_vcore_cap(self):
        limits = m.instance_limits(instance(vcores=8, reserved_mb=1024 * 1024), {"gp_storage": "next-gen"})
        self.assertEqual(limits["iops"], 3072)                      # 1,024 GB x 3
        self.assertEqual(limits["throughput_mb_per_second"], 102.4)  # 3,072 / 30
        self.assertEqual(limits["log_write_mib_per_second"], 36.0)   # 4.5 x 8
        self.assertTrue(any("--mi-iops" in note for note in limits["notes"]))

    def test_stated_iops_is_used_but_never_above_the_cap(self):
        limits = m.instance_limits(instance(vcores=8), {"gp_storage": "next-gen", "iops": 20000})
        self.assertEqual(limits["iops"], 12800)
        self.assertEqual(limits["throughput_mb_per_second"], 290)
        self.assertTrue(any("above" in note for note in limits["notes"]))

    def test_next_gen_small_storage_gets_the_minimums(self):
        limits = m.instance_limits(instance(vcores=4, reserved_mb=32 * 1024), {"gp_storage": "next-gen"})
        self.assertEqual(limits["iops"], 300)
        self.assertEqual(limits["throughput_mb_per_second"], 75)

    def test_business_critical_iops_scale_with_vcores(self):
        limits = m.instance_limits(instance(sku="Business Critical", vcores=8), {})
        self.assertEqual(limits["iops"], 32000)
        self.assertIsNone(limits["throughput_mb_per_second"])
        self.assertEqual(limits["log_write_mib_per_second"], 128.0)

    def test_log_limit_is_unknown_only_when_the_hardware_matters(self):
        # 32 vCores x 4.5 = 144 MiB/s: capped at 120 on standard, 144 on premium.
        self.assertEqual(m.log_rate_limit("next-gen", 32, "standard"), (120.0, None))
        self.assertEqual(m.log_rate_limit("next-gen", 32, "premium"), (144.0, None))
        limit, reason = m.log_rate_limit("next-gen", 32, None)
        self.assertIsNone(limit)
        self.assertIn("hardware", reason)
        # 8 vCores x 4.5 = 36 MiB/s on any hardware.
        self.assertEqual(m.log_rate_limit("next-gen", 8, None), (36.0, None))

    def test_hardware_names_are_read_as_series(self):
        self.assertEqual(m.hardware_series("Gen5"), "standard")
        self.assertEqual(m.hardware_series("Gen 7"), "standard")
        self.assertEqual(m.hardware_series("Gen8"), "premium")
        self.assertIsNone(m.hardware_series("Gen4"))

    def test_general_purpose_storage_type_is_never_guessed(self):
        limits = m.instance_limits(instance(reserved_mb=20 * 1024 * 1024), {})
        self.assertIsNone(limits["storage"])
        self.assertFalse(limits["per_file_limits"])
        self.assertIsNone(limits["iops"])
        self.assertTrue(any("--mi-gp-storage" in note for note in limits["notes"]))


class ClassificationTests(unittest.TestCase):
    def test_quiet_load_is_healthy(self):
        result = run(quiet_load())
        self.assertEqual(result["classification"], "HEALTHY")
        self.assertEqual(result["limits_version"], m.LIMITS_VERSION)

    def test_governor_waits_prove_an_instance_limit(self):
        result = run(quiet_load(), governor_ms=12000)
        self.assertEqual(result["classification"], "INSTANCE_LEVEL")
        self.assertTrue(any("INSTANCE_LOG_RATE_GOVERNOR" in line for line in result["evidence"]))

    def test_log_rate_near_the_limit_is_instance_level(self):
        # 8 vCores classic GP: 36 MiB/s limit. 3.2 GiB in 100 s is about 32.8 MiB/s.
        files = quiet_load()
        files[1]["num_of_bytes_written"] = int(3.2 * GIB)
        result = run(files)
        self.assertGreaterEqual(result["utilisation_percent"]["log_write"], 70)
        self.assertEqual(result["classification"], "INSTANCE_LEVEL")

    def test_busy_slow_file_is_file_level(self):
        files = quiet_load()
        files[0].update(num_of_writes=45000, io_stall_write_ms=45000 * 20)  # 450 of 500 IOPS, 20 ms
        result = run(files)
        self.assertEqual(result["classification"], "FILE_LEVEL")
        busiest = result["files"][0]
        self.assertEqual((busiest["iops_limit"], busiest["iops_percent"]), (500, 90.0))

    def test_file_and_instance_pressure_together_is_mixed(self):
        files = quiet_load()
        files[0].update(num_of_writes=45000, io_stall_write_ms=45000 * 20)
        self.assertEqual(run(files, governor_ms=500)["classification"], "MIXED")

    def test_slow_io_without_a_limit_is_not_proven(self):
        files = quiet_load()
        files[0]["io_stall_write_ms"] = 10000 * 30  # 30 ms, but only 100 IOPS
        self.assertEqual(run(files)["classification"], "NOT_PROVEN")

    def test_unknown_storage_type_still_uses_the_waits(self):
        result = run(quiet_load(), stated={}, governor_ms=100)
        self.assertEqual(result["classification"], "INSTANCE_LEVEL")
        self.assertIsNone(result["files"][0]["iops_limit"])

    def test_without_the_storage_type_a_quiet_load_is_not_called_healthy(self):
        result = run(quiet_load(), stated={})
        self.assertEqual(result["classification"], "NOT_ASSESSED")
        # The log limit is still known: both General Purpose types give 36 MiB/s at 8 vCores.
        self.assertEqual(result["limits"]["log_write_mib_per_second"], 36.0)

    def test_no_io_and_nothing_collected(self):
        self.assertEqual(run([])["classification"], "NO_ACTIVITY")
        result = m.assess({"instance": instance(), "files": None, "not_collected": {"files": "denied"}},
                          {"instance": instance(), "files": None, "not_collected": {"files": "denied"}},
                          WINDOW, waits(), {"gp_storage": "classic"}, "LoadTest")
        self.assertEqual(result["classification"], "NOT_ASSESSED")
        self.assertEqual(result["not_collected"]["files"], "denied")

    def test_missing_snapshot_is_not_assessed(self):
        result = m.assess(None, None, WINDOW, waits(), {}, "LoadTest")
        self.assertFalse(result["collected"])
        self.assertEqual(result["classification"], "NOT_ASSESSED")


class InstanceWideTests(unittest.TestCase):
    def test_other_busy_database_is_warned_about(self):
        files = quiet_load() + [file_row(6, "Reporting", 1, "Reporting", "ROWS", 50, reads=40000)]
        result = run(files)
        self.assertEqual(result["databases"][0]["database_name"], "Reporting")
        self.assertTrue(any(d["is_target"] for d in result["databases"]))
        self.assertTrue(any("Reporting" in w for w in result["warnings"]))

    def test_tempdb_is_outside_general_purpose_limits_but_inside_business_critical(self):
        files = quiet_load() + [file_row(2, "tempdb", 1, "tempdev", "ROWS", 10, writes=10000)]
        gp = run(files)
        self.assertEqual(gp["observed"]["iops"], 150.0)                 # tempdb excluded
        self.assertIsNone([f for f in gp["files"] if f["database_name"] == "tempdb"][0]["iops_limit"])
        bc = run(files, inst=instance(sku="Business Critical"))
        self.assertEqual(bc["observed"]["iops"], 250.0)                 # tempdb included

    def test_reset_counters_are_excluded_and_reported(self):
        after = quiet_load()
        before = [dict(f, **{k: 0 for k in m.FILE_COUNTERS}) for f in after]
        before[0]["num_of_writes"] = 10 ** 9
        result = run(after, before_files=before)
        self.assertEqual(len(result["files"]), 1)
        self.assertTrue(any("reset" in w for w in result["warnings"]))

    def test_scaling_during_the_test_is_warned_about(self):
        result = run(quiet_load(), after_inst=instance(vcores=16))
        self.assertTrue(any("changed during the test" in w for w in result["warnings"]))


class CaptureTests(unittest.TestCase):
    def test_a_part_that_cannot_be_read_is_recorded_not_raised(self):
        def fetch(_connection, sql):
            if "server_resource_stats" in sql:
                raise PermissionError("VIEW SERVER STATE permission was denied")
            return [{"database_id": 5, "database_name": "LoadTest", "file_id": 1, "logical_file_name": "LoadTest",
                     "file_type": "ROWS", "size_bytes": GIB, **{k: 1 for k in m.FILE_COUNTERS}}]

        snapshot = m.capture_mi_snapshot(object(), fetch)
        self.assertIsNone(snapshot["instance"])
        self.assertIn("VIEW SERVER STATE", snapshot["not_collected"]["instance"])
        self.assertEqual(len(snapshot["files"]), 1)

    def test_queries_only_read(self):
        for sql in (m.SQL_MI_INSTANCE, m.SQL_MI_INSTANCE_FILES):
            self.assertTrue(sql.strip().upper().startswith("SELECT"))
            for word in ("INSERT", "UPDATE", "DELETE", "ALTER", "EXEC", "USE "):
                self.assertNotIn(word, sql.upper())


if __name__ == "__main__":
    unittest.main()

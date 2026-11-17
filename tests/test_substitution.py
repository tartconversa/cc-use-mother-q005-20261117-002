import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from cycleflow.api import create_app
from cycleflow.contracts import DomainError, configuration_id
from cycleflow.demo import DAY, END, START
from tests.common import ServiceCase


class SubstitutionCase(ServiceCase):
    def seed_inventory(self, motors=6, sensors=10, alt=10):
        self.seed_design()
        self.ingest(2, "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                          "supplier": "motors", "quantity": motors})
        self.ingest(3, "lot.receive", 2, {"lot_id": "S1", "part": "sensor",
                                          "supplier": "sensors", "quantity": sensors})
        self.seed_plan()
        if alt:
            self.ingest(5, "lot.receive", 3, {"lot_id": "A1", "part": "motor-alt",
                                              "supplier": "alt-motors", "quantity": alt})

    def approve(self, number=8, minute=4, **overrides):
        payload = {"approval_id": "AP-1", "design_id": "R1",
                   "original_part": "motor", "substitute_part": "motor-alt",
                   "valid_from": f"{DAY}T09:{minute:02d}:00Z",
                   "valid_until": f"{DAY}T09:50:00Z",
                   "approved_by": "eng-x", "reason": "motor shortage"}
        payload.update(overrides)
        return self.ingest(number, "substitute.approve", minute, payload)

    def open_job(self, job_id, quantity, number, minute, **extra):
        payload = {"job_id": job_id, "design_id": "R1", "quantity": quantity}
        payload.update(extra)
        return self.ingest(number, "job.open", minute, payload)

    def run_stages(self, job_id, number_base, start_minute):
        for offset, stage in enumerate(("machining", "assembly", "integration")):
            self.ingest(number_base + offset * 2, "job.stage_start", start_minute + offset * 6,
                        {"job_id": job_id, "stage": stage})
            self.ingest(number_base + offset * 2 + 1, "job.stage_finish", start_minute + offset * 6 + 3,
                        {"job_id": job_id, "stage": stage})


class SubstitutionTests(SubstitutionCase):
    def test_approve_then_job_records_actual_configuration(self):
        self.seed_inventory()
        approved = self.approve()
        self.assertEqual(approved["state"], "accepted")
        opened = self.open_job("J1", 4, 9, 5)
        self.assertEqual(opened["state"], "accepted")
        detail = opened["detail"]
        self.assertEqual(detail["configuration"], {"motor": "motor-alt", "sensor": "sensor"})
        self.assertEqual(detail["substitutions"],
                         [{"part": "motor", "substitute_part": "motor-alt", "approval_id": "AP-1"}])
        job = self.service.get_job("J1")
        self.assertEqual(job["config_id"], detail["config_id"])
        self.assertEqual(job["configuration"]["motor"], "motor-alt")
        link = job["substitutions"][0]
        self.assertEqual(link["approval_id"], "AP-1")
        self.assertEqual(link["approved_by"], "eng-x")
        self.assertIsNone(link["revoked_at"])
        self.assertEqual({(r["lot_id"], r["quantity"]) for r in job["reservations"]},
                         {("A1", 8), ("S1", 4)})

    def test_substitute_stock_is_not_original_inventory(self):
        self.seed_inventory()
        before = self.service.capacity("R1", 4, f"{DAY}T09:03:30Z")
        self.assertFalse(before["feasible"])
        self.assertEqual(before["max_units"], 3)
        motor = next(item for item in before["requirements"] if item["part"] == "motor")
        self.assertEqual(motor["available"], 6)
        self.assertEqual(motor["shortage"], 2)
        self.assertIsNone(motor["substitution"])
        self.approve()
        after = self.service.capacity("R1", 4, f"{DAY}T09:05:00Z")
        motor = next(item for item in after["requirements"] if item["part"] == "motor")
        self.assertEqual(motor["substitution"]["approval_id"], "AP-1")
        self.assertEqual(motor["substitution"]["available"], 10)
        self.assertTrue(after["feasible"])
        self.assertEqual(after["max_units"], 5)
        opened = self.open_job("J1", 4, 9, 5)
        self.assertEqual(opened["detail"]["config_id"],
                         after["resolved_configuration"]["config_id"])
        self.assertEqual(self.service.get_lot("M1")["available"], 6)
        self.assertEqual(self.service.get_lot("A1")["available"], 2)

    def test_job_opened_before_approval_is_not_backdated(self):
        self.seed_inventory()
        waiting = self.open_job("J1", 4, 8, 3)
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["error_code"], "shortage")
        self.approve(number=9)
        retried = self.service.retry(waiting["id"])
        self.assertEqual(retried["state"], "waiting")
        self.assertIn("AP-1", retried["error_text"])
        opened = self.open_job("J2", 4, 10, 5)
        self.assertEqual(opened["state"], "accepted")
        self.assertEqual(opened["detail"]["configuration"]["motor"], "motor-alt")

    def test_approval_validity_boundary_is_half_open(self):
        self.seed_inventory(motors=1, alt=20)
        self.approve(valid_until=f"{DAY}T09:10:00Z")
        inside = self.open_job("J1", 2, 9, 9)
        self.assertEqual(inside["state"], "accepted")
        self.assertEqual(inside["detail"]["configuration"]["motor"], "motor-alt")
        boundary = self.open_job("J2", 2, 10, 10)
        self.assertEqual(boundary["state"], "waiting")
        self.assertIn("no active approval", boundary["error_text"])

    def test_revocation_is_not_retroactive(self):
        self.seed_inventory(motors=1, alt=20)
        self.approve()
        opened = self.open_job("J1", 2, 9, 5)
        revoked = self.ingest(10, "substitute.revoke", 6,
                              {"approval_id": "AP-1", "reason": "field failure",
                               "revoked_by": "eng-lead"})
        self.assertEqual(revoked["state"], "accepted")
        self.assertEqual(revoked["detail"]["jobs_already_authorized"], 1)
        approval = self.service.get_approval("AP-1")
        self.assertTrue(approval["revoked"])
        self.assertEqual(approval["revoke_reason"], "field failure")
        self.assertEqual(approval["jobs"], ["J1"])
        self.assertEqual(approval["units_consumed"], 2)
        late = self.open_job("J2", 2, 11, 7)
        self.assertEqual(late["state"], "waiting")
        self.assertIn("AP-1", late["error_text"])
        job = self.service.get_job("J1")
        self.assertEqual(job["configuration"]["motor"], "motor-alt")
        self.assertEqual(job["config_id"], opened["detail"]["config_id"])
        again = self.ingest(12, "substitute.revoke", 8, {"approval_id": "AP-1", "reason": "twice"})
        self.assertEqual(again["state"], "rejected")
        self.assertEqual(again["error_code"], "already_revoked")
        self.assertTrue(self.service.audit("robot-arm")["ok"])

    def test_max_units_caps_authorized_prototypes(self):
        self.seed_inventory(motors=1, alt=20)
        self.approve(max_units=3)
        first = self.open_job("J1", 2, 9, 5)
        self.assertEqual(first["state"], "accepted")
        over = self.open_job("J2", 2, 10, 6)
        self.assertEqual(over["state"], "waiting")
        self.assertIn("1 of 3 units left", over["error_text"])
        exact = self.open_job("J3", 1, 11, 7)
        self.assertEqual(exact["state"], "accepted")
        exhausted = self.open_job("J4", 1, 12, 8)
        self.assertEqual(exhausted["state"], "waiting")
        approval = self.service.get_approval("AP-1")
        self.assertEqual(approval["units_consumed"], 3)
        self.assertEqual(approval["remaining_units"], 0)
        self.assertEqual(approval["jobs"], ["J1", "J3"])

    def test_report_splits_yield_by_configuration(self):
        self.seed_inventory()
        original = self.open_job("J1", 2, 8, 3)
        self.assertEqual(original["detail"]["configuration"]["motor"], "motor")
        self.approve(number=9)
        substituted = self.open_job("J2", 4, 10, 5)
        self.assertEqual(substituted["detail"]["configuration"]["motor"], "motor-alt")
        self.run_stages("J1", 20, 6)
        self.run_stages("J2", 30, 22)
        self.ingest(40, "test.record", 40, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 2, "failed": 0})
        self.ingest(41, "test.record", 41, {"test_id": "T2", "job_id": "J2",
                                            "plan_id": "P1", "passed": 2, "failed": 2})
        report = self.service.report("robot-arm", START, END)
        revision = report["revisions"][0]
        self.assertTrue(revision["mixed_configurations"])
        self.assertEqual(revision["pass_rate"], "0.6666666666666666666666666667")
        configs = {entry["substituted"]: entry for entry in revision["configurations"]}
        self.assertEqual(configs[False]["pass_rate"], "1")
        self.assertEqual(configs[False]["tested_units"], 2)
        self.assertEqual(configs[True]["pass_rate"], "0.5")
        self.assertEqual(configs[True]["config_id"], substituted["detail"]["config_id"])
        self.assertEqual(configs[True]["approvals"],
                         [{"approval_id": "AP-1", "original_part": "motor",
                           "substitute_part": "motor-alt"}])
        job_entries = {job["job_id"]: job for job in report["jobs"]}
        self.assertEqual(job_entries["J2"]["config_id"], substituted["detail"]["config_id"])
        self.assertEqual(job_entries["J2"]["substitutions"][0]["approval_id"], "AP-1")

    def test_trace_links_quality_to_approval_lots_and_job(self):
        self.seed_inventory()
        approved = self.approve()
        opened = self.open_job("J1", 4, 9, 5)
        config_id = opened["detail"]["config_id"]
        self.run_stages("J1", 20, 6)
        self.ingest(30, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 1, "failed": 3})
        trace = self.service.get_test("T1")
        self.assertEqual(trace["configuration"]["config_id"], config_id)
        self.assertEqual(trace["configuration"]["mapping"]["motor"], "motor-alt")
        self.assertEqual(trace["substitutions"][0]["approval_id"], "AP-1")
        self.assertEqual({r["lot_id"] for r in trace["reservations"]}, {"A1", "S1"})
        self.assertEqual(trace["job_lineage"][0]["config_id"], config_id)
        approval = self.service.get_approval("AP-1")
        self.assertEqual(approval["jobs"], ["J1"])
        self.assertEqual(approval["units_consumed"], 4)
        lineage = self.service.get_lineage(approved["id"])
        self.assertEqual(lineage["trace"]["approval_id"], "AP-1")
        self.assertEqual(lineage["trace"]["jobs"], ["J1"])

    def test_rework_after_revocation_uses_its_own_configuration(self):
        self.seed_inventory(motors=4, alt=10)
        self.approve()
        first = self.open_job("J1", 4, 9, 5)
        self.assertEqual(first["detail"]["configuration"]["motor"], "motor-alt")
        self.run_stages("J1", 20, 6)
        self.ingest(30, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 1, "failed": 3})
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        self.ingest(31, "substitute.revoke", 35, {"approval_id": "AP-1", "reason": "substitute drift"})
        rework = self.open_job("J2", 2, 32, 36, parent_job="J1")
        self.assertEqual(rework["state"], "accepted")
        self.assertEqual(rework["detail"]["configuration"]["motor"], "motor")
        self.assertNotEqual(rework["detail"]["config_id"], first["detail"]["config_id"])
        timeline = self.service.job_timeline("J2")
        self.assertEqual([entry["config_id"] for entry in timeline["lineage"]],
                         [rework["detail"]["config_id"], first["detail"]["config_id"]])

    def test_quarantine_isolates_substitute_lot(self):
        self.seed_inventory(motors=1, alt=10)
        self.approve()
        isolated = self.ingest(9, "lot.quarantine", 5, {"lot_id": "A1", "quarantined": True})
        self.assertEqual(isolated["state"], "accepted")
        waiting = self.open_job("J1", 2, 10, 6)
        self.assertEqual(waiting["state"], "waiting")
        self.assertIn("motor-alt", waiting["error_text"])
        self.ingest(11, "lot.quarantine", 7, {"lot_id": "A1", "quarantined": False})
        retried = self.service.retry(waiting["id"])
        self.assertEqual(retried["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["configuration"]["motor"], "motor-alt")
        in_use = self.ingest(12, "lot.quarantine", 8, {"lot_id": "A1", "quarantined": True})
        self.assertEqual(in_use["error_code"], "lot_in_use")

    def test_late_pickup_is_judged_at_business_time(self):
        self.seed_inventory(motors=1, alt=20)
        self.approve(valid_until=f"{DAY}T09:20:00Z")
        self.ingest(9, "substitute.revoke", 8, {"approval_id": "AP-1", "reason": "window closed early"})
        inside = self.open_job("J1", 2, 10, 5)
        self.assertEqual(inside["state"], "accepted")
        self.assertEqual(inside["detail"]["configuration"]["motor"], "motor-alt")
        after = self.open_job("J2", 2, 11, 9)
        self.assertEqual(after["state"], "waiting")

    def test_lot_received_after_job_time_requires_new_job(self):
        self.seed_inventory(motors=1, alt=0)
        self.approve()
        waiting = self.open_job("J1", 2, 9, 5)
        self.assertEqual(waiting["state"], "waiting")
        self.assertIn("motor-alt", waiting["error_text"])
        self.assertIn("AP-1", waiting["error_text"])
        self.ingest(10, "lot.receive", 6, {"lot_id": "A1", "part": "motor-alt",
                                           "supplier": "alt-motors", "quantity": 10})
        retried = self.service.retry(waiting["id"])
        self.assertEqual(retried["state"], "waiting")
        opened = self.open_job("J2", 2, 11, 7)
        self.assertEqual(opened["state"], "accepted")
        self.assertEqual(opened["detail"]["configuration"]["motor"], "motor-alt")

    def test_approval_validation_rules(self):
        self.seed_inventory()
        missing = self.ingest(8, "substitute.approve", 4,
                              {"approval_id": "AP-X", "design_id": "RX",
                               "original_part": "motor", "substitute_part": "motor-alt",
                               "valid_from": f"{DAY}T09:04:00Z", "valid_until": f"{DAY}T09:50:00Z",
                               "approved_by": "eng-x", "reason": "motor shortage"})
        self.assertEqual(missing["state"], "waiting")
        self.assertEqual(missing["error_code"], "missing_dependency")
        unknown = self.approve(number=9, original_part="gear")
        self.assertEqual(unknown["error_code"], "unknown_part")
        same = self.approve(number=10, substitute_part="motor")
        self.assertEqual(same["error_code"], "invalid_substitute")
        bom_part = self.approve(number=11, substitute_part="sensor")
        self.assertEqual(bom_part["error_code"], "invalid_substitute")
        backdated = self.approve(number=12, valid_from=f"{DAY}T09:00:00Z")
        self.assertEqual(backdated["error_code"], "backdated_validity")
        inverted = self.approve(number=13, valid_until=f"{DAY}T09:04:00Z")
        self.assertEqual(inverted["error_code"], "invalid_validity")
        first = self.approve(number=14)
        self.assertEqual(first["state"], "accepted")
        replay = self.approve(number=14)
        self.assertTrue(replay["repeated"])
        overlap = self.approve(number=15, approval_id="AP-2",
                               valid_from=f"{DAY}T09:20:00Z", valid_until=f"{DAY}T09:55:00Z")
        self.assertEqual(overlap["error_code"], "overlapping_approval")
        adjacent = self.approve(number=16, minute=45, approval_id="AP-2",
                                valid_from=f"{DAY}T09:50:00Z", valid_until=f"{DAY}T09:55:00Z")
        self.assertEqual(adjacent["state"], "accepted")
        self.ingest(17, "substitute.revoke", 6, {"approval_id": "AP-1", "reason": "superseded"})
        freed = self.approve(number=18, minute=6, approval_id="AP-3",
                             valid_from=f"{DAY}T09:06:00Z", valid_until=f"{DAY}T09:45:00Z")
        self.assertEqual(freed["state"], "accepted")

    def test_configuration_view_aggregates_jobs_lots_and_quality(self):
        self.seed_inventory(motors=1, alt=20)
        self.approve()
        first = self.open_job("J1", 1, 9, 5)
        second = self.open_job("J2", 1, 10, 6)
        config_id = first["detail"]["config_id"]
        self.assertEqual(config_id, second["detail"]["config_id"])
        self.run_stages("J1", 20, 7)
        self.run_stages("J2", 30, 24)
        self.ingest(40, "test.record", 40, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 1, "failed": 0})
        self.ingest(41, "test.record", 41, {"test_id": "T2", "job_id": "J2",
                                            "plan_id": "P1", "passed": 0, "failed": 1})
        config = self.service.get_configuration(config_id)
        self.assertTrue(config["substituted"])
        self.assertEqual([job["job_id"] for job in config["jobs"]], ["J1", "J2"])
        self.assertEqual(config["tested_units"], 2)
        self.assertEqual(config["pass_rate"], "0.5")
        self.assertEqual(config["substitutions"][0]["approval_id"], "AP-1")
        self.assertIn("A1", {lot["lot_id"] for lot in config["lots"]})
        with self.assertRaises(DomainError) as caught:
            self.service.get_configuration(
                configuration_id("R1", {"motor": "motor", "sensor": "sensor"}))
        self.assertEqual(caught.exception.code, "missing_dependency")

    def test_release_freezes_configuration_breakdown(self):
        self.seed_inventory()
        self.open_job("J1", 2, 8, 3)
        self.approve(number=9)
        self.open_job("J2", 4, 10, 5)
        self.run_stages("J1", 20, 6)
        self.run_stages("J2", 30, 22)
        self.ingest(40, "test.record", 40, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 2, "failed": 0})
        self.ingest(41, "test.record", 41, {"test_id": "T2", "job_id": "J2",
                                            "plan_id": "P1", "passed": 3, "failed": 1})
        report = self.service.report("robot-arm", START, END)
        self.assertTrue(report["quality"]["publishable"])
        frozen = self.service.publish("jan", "robot-arm", START, END, report["fingerprint"])
        revision = frozen["report"]["revisions"][0]
        self.assertTrue(revision["mixed_configurations"])
        self.assertEqual(len(revision["configurations"]), 2)
        self.ingest(42, "lot.receive", 45, {"lot_id": "M2", "part": "motor",
                                            "supplier": "motors", "quantity": 4})
        self.assertTrue(self.service.shelf.freshness("jan")["current_differs"])
        audit = self.service.audit("robot-arm")
        self.assertTrue(audit["ok"], audit)

    def test_http_substitution_and_configuration_endpoints(self):
        client = TestClient(create_app(str(self.path)))
        self.seed_inventory()
        self.approve()
        opened = self.open_job("J1", 4, 9, 5)
        approval = client.get("/substitutions/AP-1")
        self.assertEqual(approval.status_code, 200)
        self.assertEqual(approval.json()["jobs"], ["J1"])
        self.assertEqual(approval.json()["units_consumed"], 4)
        configuration = client.get(f"/configurations/{opened['detail']['config_id']}")
        self.assertEqual(configuration.status_code, 200)
        self.assertEqual(configuration.json()["mapping"], {"motor": "motor-alt", "sensor": "sensor"})
        self.assertEqual(configuration.json()["jobs"][0]["job_id"], "J1")
        missing = client.get("/substitutions/AP-none")
        self.assertEqual(missing.status_code, 409)

    def test_migration_backfills_identity_configuration(self):
        path = Path(self.directory.name) / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.executescript("""
        CREATE TABLE designs (design_id TEXT PRIMARY KEY, product TEXT NOT NULL,
          revision INTEGER NOT NULL, parent_id TEXT, released_at TEXT NOT NULL,
          specification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'released');
        CREATE TABLE requirements (design_id TEXT NOT NULL, part TEXT NOT NULL,
          quantity INTEGER NOT NULL, PRIMARY KEY(design_id, part));
        CREATE TABLE jobs (job_id TEXT PRIMARY KEY, design_id TEXT NOT NULL,
          quantity INTEGER NOT NULL, opened_at TEXT NOT NULL, status TEXT NOT NULL,
          completed_at TEXT, parent_job TEXT, failure_reason TEXT);
        INSERT INTO designs VALUES('R1','robot-arm',1,NULL,'2026-01-12T09:00:00+00:00','{}','released');
        INSERT INTO requirements VALUES('R1','motor',2),('R1','sensor',1);
        INSERT INTO jobs VALUES('J1','R1',2,'2026-01-12T09:04:00+00:00','open',NULL,NULL,NULL);
        """)
        db.close()
        service = self.service.__class__(path)
        job = service.get_job("J1")
        self.assertEqual(job["configuration"], {"motor": "motor", "sensor": "sensor"})
        self.assertEqual(job["config_id"],
                         configuration_id("R1", {"motor": "motor", "sensor": "sensor"}))

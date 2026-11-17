import json
import tempfile
from pathlib import Path

from .audit import IntegrityAudit
from .service import CycleService


DAY = "2026-01-12"
START = "2026-01-01T00:00:00Z"
END = "2026-02-01T00:00:00Z"


def event(number, kind, minute, payload, source="demo"):
    return {"source": source, "external_id": str(number), "kind": kind,
            "occurred_at": f"{DAY}T09:{minute:02d}:00Z", "payload": payload}


def scenario():
    return [
        event(1, "design.release", 0, {"design_id": "R1", "product": "robot-arm",
                                       "revision": 1, "parts": {"motor": 2, "sensor": 1},
                                       "specification": {"load_kg": 5}}),
        event(2, "lot.receive", 1, {"lot_id": "M1", "part": "motor",
                                     "supplier": "local-motor", "quantity": 8}),
        event(3, "lot.receive", 2, {"lot_id": "S1", "part": "sensor",
                                     "supplier": "local-sensor", "quantity": 4}),
        event(4, "plan.create", 3, {"plan_id": "P1", "product": "robot-arm",
                                     "revision": 1, "minimum_pass_rate": "0.75"}),
        event(5, "job.open", 4, {"job_id": "J1", "design_id": "R1", "quantity": 4}),
        event(6, "job.stage_start", 5, {"job_id": "J1", "stage": "machining"}),
        event(7, "job.stage_finish", 10, {"job_id": "J1", "stage": "machining"}),
        event(8, "job.stage_start", 12, {"job_id": "J1", "stage": "assembly"}),
        event(9, "job.stage_finish", 20, {"job_id": "J1", "stage": "assembly"}),
        event(10, "job.stage_start", 21, {"job_id": "J1", "stage": "integration"}),
        event(11, "job.stage_finish", 27, {"job_id": "J1", "stage": "integration"}),
        event(12, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                      "plan_id": "P1", "passed": 3, "failed": 1,
                                      "note": "Synthetic local fixture"}),
    ]


def substitution_scenario():
    """Motor stock is exhausted by J1, so engineering approves a time-boxed
    substitute; J2 is built under that approval with its own configuration."""
    return [
        event(13, "lot.receive", 31, {"lot_id": "A1", "part": "motor-alt",
                                       "supplier": "alt-motors", "quantity": 8}),
        event(14, "lot.receive", 31, {"lot_id": "S2", "part": "sensor",
                                       "supplier": "local-sensor", "quantity": 2}),
        event(15, "substitute.approve", 32, {"approval_id": "AP1", "design_id": "R1",
                                              "original_part": "motor", "substitute_part": "motor-alt",
                                              "valid_from": f"{DAY}T09:32:00Z",
                                              "valid_until": f"{DAY}T10:30:00Z",
                                              "approved_by": "eng-demo", "max_units": 4,
                                              "reason": "motor shortage during pilot run"}),
        event(16, "job.open", 33, {"job_id": "J2", "design_id": "R1", "quantity": 2}),
        event(17, "job.stage_start", 34, {"job_id": "J2", "stage": "machining"}),
        event(18, "job.stage_finish", 38, {"job_id": "J2", "stage": "machining"}),
        event(19, "job.stage_start", 40, {"job_id": "J2", "stage": "assembly"}),
        event(20, "job.stage_finish", 44, {"job_id": "J2", "stage": "assembly"}),
        event(21, "job.stage_start", 45, {"job_id": "J2", "stage": "integration"}),
        event(22, "job.stage_finish", 50, {"job_id": "J2", "stage": "integration"}),
        event(23, "test.record", 51, {"test_id": "T2", "job_id": "J2",
                                       "plan_id": "P1", "passed": 1, "failed": 1,
                                       "note": "substitute motor run"}),
    ]


def run():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "cycleflow.sqlite3"
        service = CycleService(db)
        first = service.ingest_batch("sample", scenario(), stop_after=5)
        assert first["next_line"] == 5
        rest = service.ingest_batch("sample", scenario())
        assert rest["completed"]
        extra = service.ingest_batch("substitutes", substitution_scenario())
        assert extra["completed"]
        report = service.report("robot-arm", START, END)
        assert report["summary"]["passed_units"] == 4
        assert report["quality"]["publishable"]
        revision = report["revisions"][0]
        assert revision["mixed_configurations"]
        configs = {entry["substituted"]: entry for entry in revision["configurations"]}
        assert configs[False]["pass_rate"] == "0.75"
        assert configs[True]["pass_rate"] == "0.5"
        assert configs[True]["approvals"][0]["approval_id"] == "AP1"
        job = service.get_job("J2")
        assert job["configuration"] == {"motor": "motor-alt", "sensor": "sensor"}
        preview = service.capacity("R1", 2, f"{DAY}T09:33:00Z")
        assert preview["resolved_configuration"]["config_id"] == job["config_id"]
        approval = service.get_approval("AP1")
        assert approval["units_consumed"] == 2 and approval["remaining_units"] == 2
        configuration = service.get_configuration(job["config_id"])
        assert configuration["jobs"][0]["job_id"] == "J2"
        frozen = service.publish("jan-demo", "robot-arm", START, END, report["fingerprint"])
        assert frozen["report"]["summary"]["pass_rate"] == report["summary"]["pass_rate"]
        audit = IntegrityAudit(service.store).run("robot-arm")
        assert audit["ok"], audit
        return {"batch": {"first_cursor": first["next_line"], "complete": rest["completed"]},
                "report": report["summary"], "quality": report["quality"],
                "configurations": revision["configurations"],
                "release_id": frozen["release_id"], "audit": audit}


def main():
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

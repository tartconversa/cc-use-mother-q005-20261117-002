import json

from .contracts import (DomainError, MissingDependency, configuration_id, keys,
                        nonnegative_int, required_text, stable_json, timestamp)


class SubstitutionRules:
    """Time-boxed, design-scoped approvals that let a job cover a required
    part with a substitute part. Approvals never rewrite history: jobs that
    already opened keep the configuration recorded at their own open time."""

    def __init__(self, db):
        self.db = db

    def approve(self, payload, occurred_at, event_id):
        keys(payload,
             {"approval_id", "design_id", "original_part", "substitute_part",
              "valid_from", "valid_until", "approved_by", "reason"},
             {"approval_id", "design_id", "original_part", "substitute_part",
              "valid_from", "valid_until", "approved_by", "reason", "max_units"})
        approval_id = required_text(payload["approval_id"], "approval_id")
        design_id = required_text(payload["design_id"], "design_id")
        original = required_text(payload["original_part"], "original_part")
        substitute = required_text(payload["substitute_part"], "substitute_part")
        approved_by = required_text(payload["approved_by"], "approved_by")
        reason = required_text(payload["reason"], "reason")
        valid_from = timestamp(payload["valid_from"], "valid_from")
        valid_until = timestamp(payload["valid_until"], "valid_until")
        max_units = payload.get("max_units")
        if max_units is not None:
            max_units = nonnegative_int(max_units, "max_units", positive=True)
        if valid_until <= valid_from:
            raise DomainError("invalid_validity", "valid_until must follow valid_from")
        if valid_from < occurred_at:
            raise DomainError("backdated_validity", "valid_from precedes the approval time")
        design = self.db.execute("SELECT * FROM designs WHERE design_id=?", (design_id,)).fetchone()
        if design is None:
            raise MissingDependency("design", design_id)
        parts = {row["part"] for row in self.db.execute(
            "SELECT part FROM requirements WHERE design_id=?", (design_id,))}
        if original not in parts:
            raise DomainError("unknown_part", f"{original} is not required by {design_id}")
        if substitute == original or substitute in parts:
            raise DomainError("invalid_substitute",
                              "substitute must differ from the original and stay outside the design BOM")
        for other in self.db.execute(
                "SELECT approval_id,valid_from,valid_until,revoked_at FROM substitutions"
                " WHERE design_id=? AND original_part=?", (design_id, original)):
            effective_end = min(other["valid_until"], other["revoked_at"] or other["valid_until"])
            if other["valid_from"] < valid_until and valid_from < effective_end:
                raise DomainError("overlapping_approval",
                                  f"validity overlaps {other['approval_id']} for the same part")
        self.db.execute(
            "INSERT INTO substitutions(approval_id,design_id,original_part,substitute_part,"
            "max_units,valid_from,valid_until,approved_by,reason,approved_at,event_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, design_id, original, substitute, max_units, valid_from, valid_until,
             approved_by, reason, occurred_at, event_id))
        return {"approval_id": approval_id, "design_id": design_id,
                "original_part": original, "substitute_part": substitute,
                "valid_from": valid_from, "valid_until": valid_until}

    def revoke(self, payload, occurred_at, event_id):
        keys(payload, {"approval_id", "reason"}, {"approval_id", "reason", "revoked_by"})
        approval_id = required_text(payload["approval_id"], "approval_id")
        reason = required_text(payload["reason"], "reason")
        revoked_by = payload.get("revoked_by")
        if revoked_by is not None:
            revoked_by = required_text(revoked_by, "revoked_by")
        row = self.db.execute("SELECT * FROM substitutions WHERE approval_id=?",
                              (approval_id,)).fetchone()
        if row is None:
            raise MissingDependency("approval", approval_id)
        if row["revoked_at"] is not None:
            raise DomainError("already_revoked", "approval already revoked")
        if occurred_at < row["approved_at"]:
            raise DomainError("time_order", "revocation precedes approval")
        self.db.execute(
            "UPDATE substitutions SET revoked_at=?,revoke_reason=?,revoked_by=?,revoke_event_id=?"
            " WHERE approval_id=?", (occurred_at, reason, revoked_by, event_id, approval_id))
        authorized = self.db.execute(
            "SELECT COUNT(*) FROM job_substitutions WHERE approval_id=?", (approval_id,)).fetchone()[0]
        return {"approval_id": approval_id, "revoked_at": occurred_at,
                "jobs_already_authorized": authorized,
                "note": "revocation is not retroactive; recorded configurations stand"}

    def get(self, approval_id):
        row = self.db.execute("SELECT * FROM substitutions WHERE approval_id=?",
                              (approval_id,)).fetchone()
        if row is None:
            raise MissingDependency("approval", approval_id)
        result = dict(row)
        result["revoked"] = result["revoked_at"] is not None
        result["units_consumed"] = self.consumed(approval_id)
        result["remaining_units"] = (None if result["max_units"] is None
                                     else result["max_units"] - result["units_consumed"])
        result["jobs"] = [r["job_id"] for r in self.db.execute(
            "SELECT job_id FROM job_substitutions WHERE approval_id=? ORDER BY job_id",
            (approval_id,))]
        return result

    def consumed(self, approval_id):
        return self.db.execute(
            "SELECT COALESCE(SUM(j.quantity),0) FROM job_substitutions js"
            " JOIN jobs j ON j.job_id=js.job_id WHERE js.approval_id=?",
            (approval_id,)).fetchone()[0]

    def remaining(self, approval):
        if approval["max_units"] is None:
            return None
        return approval["max_units"] - self.consumed(approval["approval_id"])

    def active_for(self, design_id, part, at):
        """The single approval usable at business time `at`, if any. Overlap
        prevention at approve time keeps this unambiguous."""
        row = self.db.execute(
            "SELECT * FROM substitutions WHERE design_id=? AND original_part=?"
            " AND valid_from<=? AND valid_until>? AND (revoked_at IS NULL OR revoked_at>?)"
            " ORDER BY valid_from DESC, approval_id",
            (design_id, part, at, at, at)).fetchone()
        return dict(row) if row is not None else None

    def known_for(self, design_id, part):
        return [dict(row) for row in self.db.execute(
            "SELECT approval_id,valid_from,valid_until,revoked_at FROM substitutions"
            " WHERE design_id=? AND original_part=? ORDER BY valid_from,approval_id",
            (design_id, part))]

    def ensure_configuration(self, design_id, mapping, at):
        config_id = configuration_id(design_id, mapping)
        exists = self.db.execute("SELECT 1 FROM configurations WHERE config_id=?",
                                 (config_id,)).fetchone()
        if exists is None:
            self.db.execute(
                "INSERT INTO configurations(config_id,design_id,mapping,created_at) VALUES(?,?,?,?)",
                (config_id, design_id, stable_json(mapping), at))
        return config_id

    def mapping_for(self, config_id):
        if config_id is None:
            return None
        row = self.db.execute("SELECT mapping FROM configurations WHERE config_id=?",
                              (config_id,)).fetchone()
        return json.loads(row["mapping"]) if row is not None else None

    def job_substitutions(self, job_id):
        return [dict(row) for row in self.db.execute(
            "SELECT js.part,js.substitute_part,js.approval_id,s.approved_by,s.reason,"
            "s.valid_from,s.valid_until,s.revoked_at FROM job_substitutions js"
            " JOIN substitutions s ON s.approval_id=js.approval_id"
            " WHERE js.job_id=? ORDER BY js.part", (job_id,))]

    def get_configuration(self, config_id):
        row = self.db.execute("SELECT * FROM configurations WHERE config_id=?",
                              (config_id,)).fetchone()
        if row is None:
            raise MissingDependency("configuration", config_id)
        result = dict(row)
        result["mapping"] = json.loads(result["mapping"])
        result["substituted"] = any(part != actual for part, actual in result["mapping"].items())
        jobs = [dict(r) for r in self.db.execute(
            "SELECT job_id,status,quantity,opened_at,completed_at,parent_job FROM jobs"
            " WHERE config_id=? ORDER BY opened_at,job_id", (config_id,))]
        result["jobs"] = jobs
        tested = passed = failed = 0
        for job in jobs:
            for test in self.db.execute(
                    "SELECT passed,failed FROM tests WHERE job_id=?", (job["job_id"],)):
                tested += test["passed"] + test["failed"]
                passed += test["passed"]
                failed += test["failed"]
        result["tested_units"] = tested
        result["passed_units"] = passed
        result["failed_units"] = failed
        result["pass_rate"] = (str(passed / tested) if tested else None)
        result["substitutions"] = [dict(r) for r in self.db.execute(
            "SELECT DISTINCT js.part,js.substitute_part,js.approval_id FROM job_substitutions js"
            " JOIN jobs j ON j.job_id=js.job_id WHERE j.config_id=? ORDER BY js.part",
            (config_id,))]
        result["lots"] = [dict(r) for r in self.db.execute(
            "SELECT DISTINCT r.lot_id,l.part,l.supplier FROM reservations r"
            " JOIN lots l ON l.lot_id=r.lot_id JOIN jobs j ON j.job_id=r.job_id"
            " WHERE j.config_id=? ORDER BY r.lot_id", (config_id,))]
        return result

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "substitute.approve":
            return self.approve(payload, occurred_at, event_id)
        if kind == "substitute.revoke":
            return self.revoke(payload, occurred_at, event_id)
        raise DomainError("unknown_kind", f"unsupported substitution event {kind}")

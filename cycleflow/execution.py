from .contracts import DomainError, MissingDependency, duration_seconds, keys, nonnegative_int, required_text
from .design import DesignRules
from .inventory import InventoryRules
from .substitution import SubstitutionRules


STAGES = ("machining", "assembly", "integration")


class ExecutionRules:
    def __init__(self, db):
        self.db = db
        self.designs = DesignRules(db)
        self.inventory = InventoryRules(db)
        self.substitutions = SubstitutionRules(db)

    def resolve_configuration(self, design, quantity, occurred_at):
        """Map every required part to the actual part a job of `quantity` would
        use at `occurred_at`: original stock first, then a substitute covered by
        an approval valid at that business time. One job gets one configuration,
        so yield stays attributable. Raises an explained shortage otherwise."""
        mapping, allocations, used = {}, [], {}
        explanations = []
        for part, per_unit in sorted(design["parts"].items()):
            required = per_unit * quantity
            available = self.inventory.available_at(part, occurred_at)
            if available >= required:
                mapping[part] = part
                allocations.extend(self.inventory.choose(part, required, occurred_at))
                continue
            approval = self.substitutions.active_for(design["design_id"], part, occurred_at)
            reason = None
            if approval is None:
                known = self.substitutions.known_for(design["design_id"], part)
                reason = "no active approval"
                if known:
                    windows = ", ".join(
                        f"{item['approval_id']}[{item['valid_from']}..{item['valid_until']}"
                        + (f" revoked@{item['revoked_at']}" if item["revoked_at"] else "")
                        for item in known)
                    reason += f" at {occurred_at} ({windows})"
            else:
                remaining = self.substitutions.remaining(approval)
                if remaining is not None and remaining < quantity:
                    reason = (f"approval {approval['approval_id']} has {remaining} of"
                              f" {approval['max_units']} units left, job needs {quantity}")
                else:
                    substitute = approval["substitute_part"]
                    sub_available = self.inventory.available_at(substitute, occurred_at)
                    if sub_available < required:
                        reason = (f"substitute {substitute} under {approval['approval_id']} short:"
                                  f" need {required}, available {sub_available}")
            if reason is not None:
                explanations.append(f"{part}: need {required}, original available {available}; {reason}")
                continue
            mapping[part] = approval["substitute_part"]
            used[part] = approval
            allocations.extend(self.inventory.choose(approval["substitute_part"], required, occurred_at))
        if explanations:
            raise DomainError("shortage", "; ".join(explanations), 409)
        return mapping, allocations, used

    def open(self, payload, occurred_at):
        keys(payload, {"job_id", "design_id", "quantity"},
             {"job_id", "design_id", "quantity", "parent_job"})
        job_id = required_text(payload["job_id"], "job_id")
        design_id = required_text(payload["design_id"], "design_id")
        quantity = nonnegative_int(payload["quantity"], "quantity", positive=True)
        design = self.designs.get(design_id)
        if design["state"] != "released":
            raise DomainError("retired_design", "cannot open a job on retired design")
        if occurred_at < design["released_at"]:
            raise DomainError("time_order", "job precedes design release")
        parent_id = payload.get("parent_job")
        if parent_id is not None:
            parent_id = required_text(parent_id, "parent_job")
            parent = self.get(parent_id)
            if parent["status"] not in {"failed", "scrapped"}:
                raise DomainError("invalid_rework", "parent job must have failed or been scrapped")
            if parent["design"]["product"] != design["product"]:
                raise DomainError("invalid_rework", "rework must stay in same product")
            if occurred_at < parent["opened_at"]:
                raise DomainError("time_order", "rework precedes original job")
        mapping, allocations, used = self.resolve_configuration(design, quantity, occurred_at)
        config_id = self.substitutions.ensure_configuration(design_id, mapping, occurred_at)
        self.db.execute("INSERT INTO jobs(job_id,design_id,quantity,opened_at,status,parent_job,config_id)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (job_id, design_id, quantity, occurred_at, "open", parent_id, config_id))
        recorded = self.inventory.insert_reservations(job_id, allocations)
        for part, approval in sorted(used.items()):
            self.db.execute(
                "INSERT INTO job_substitutions(job_id,part,substitute_part,approval_id) VALUES(?,?,?,?)",
                (job_id, part, approval["substitute_part"], approval["approval_id"]))
        return {"job_id": job_id, "status": "open", "config_id": config_id,
                "configuration": mapping, "allocations": recorded,
                "substitutions": [{"part": part, "substitute_part": approval["substitute_part"],
                                   "approval_id": approval["approval_id"]}
                                  for part, approval in sorted(used.items())]}

    def start_stage(self, payload, occurred_at, event_id):
        keys(payload, {"job_id", "stage"})
        job_id = required_text(payload["job_id"], "job_id")
        stage = required_text(payload["stage"], "stage")
        if stage not in STAGES:
            raise DomainError("unknown_stage", f"unsupported stage {stage}")
        job = self.get(job_id)
        if job["status"] in {"completed", "failed", "scrapped"}:
            raise DomainError("terminal_job", "terminal job cannot start stage")
        if occurred_at < job["opened_at"]:
            raise DomainError("time_order", "stage precedes job open")
        index = STAGES.index(stage)
        if index:
            previous = self.db.execute("SELECT completed_at FROM job_steps WHERE job_id=? AND stage=?",
                                       (job_id, STAGES[index - 1])).fetchone()
            if previous is None or previous["completed_at"] is None:
                raise DomainError("stage_order", "previous stage is not complete", 409)
            if occurred_at < previous["completed_at"]:
                raise DomainError("time_order", "stage overlaps previous stage")
        self.db.execute("INSERT INTO job_steps(job_id,stage,started_at,event_id) VALUES(?,?,?,?)",
                        (job_id, stage, occurred_at, event_id))
        self.db.execute("UPDATE jobs SET status='in_progress' WHERE job_id=?", (job_id,))
        return {"job_id": job_id, "stage": stage, "started_at": occurred_at}

    def finish_stage(self, payload, occurred_at):
        keys(payload, {"job_id", "stage"})
        job_id = required_text(payload["job_id"], "job_id")
        stage = required_text(payload["stage"], "stage")
        if stage not in STAGES:
            raise DomainError("unknown_stage", f"unsupported stage {stage}")
        row = self.db.execute("SELECT * FROM job_steps WHERE job_id=? AND stage=?", (job_id, stage)).fetchone()
        if row is None:
            self.get(job_id)
            raise DomainError("stage_not_started", "stage must start before finish", 409)
        if row["completed_at"] is not None:
            raise DomainError("stage_complete", "stage already complete")
        seconds = duration_seconds(row["started_at"], occurred_at)
        self.db.execute("UPDATE job_steps SET completed_at=? WHERE job_id=? AND stage=?",
                        (occurred_at, job_id, stage))
        if stage == STAGES[-1]:
            self.db.execute("UPDATE jobs SET status='awaiting_test',completed_at=? WHERE job_id=?",
                            (occurred_at, job_id))
        return {"job_id": job_id, "stage": stage, "duration_seconds": seconds}

    def scrap(self, payload, occurred_at):
        keys(payload, {"job_id", "reason"})
        job_id = required_text(payload["job_id"], "job_id")
        reason = required_text(payload["reason"], "reason")
        job = self.get(job_id)
        if job["status"] in {"completed", "failed", "scrapped"}:
            raise DomainError("terminal_job", "cannot scrap terminal job")
        if occurred_at < job["opened_at"]:
            raise DomainError("time_order", "scrap precedes opening")
        self.db.execute("UPDATE jobs SET status='scrapped',completed_at=?,failure_reason=? WHERE job_id=?",
                        (occurred_at, reason, job_id))
        released = self.inventory.release(job_id)
        return {"job_id": job_id, "status": "scrapped", "released_units": released}

    def get(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise MissingDependency("job", job_id)
        result = dict(row)
        result["design"] = self.designs.get(result["design_id"])
        result["configuration"] = self.substitutions.mapping_for(result["config_id"])
        result["substitutions"] = self.substitutions.job_substitutions(job_id)
        result["reservations"] = self.inventory.reservations(job_id)
        result["stages"] = [dict(r) for r in self.db.execute(
            "SELECT stage,started_at,completed_at,event_id FROM job_steps WHERE job_id=? ORDER BY started_at,stage", (job_id,))]
        return result

    def lineage(self, job_id):
        current = job_id
        result = []
        seen = set()
        while current:
            if current in seen:
                raise DomainError("cyclic_rework", "job lineage is cyclic")
            seen.add(current)
            job = self.get(current)
            result.append({"job_id": current, "status": job["status"],
                           "design_id": job["design_id"], "config_id": job["config_id"]})
            current = job["parent_job"]
        return result

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "job.open":
            return self.open(payload, occurred_at)
        if kind == "job.stage_start":
            return self.start_stage(payload, occurred_at, event_id)
        if kind == "job.stage_finish":
            return self.finish_stage(payload, occurred_at)
        if kind == "job.scrap":
            return self.scrap(payload, occurred_at)
        raise DomainError("unknown_kind", f"unsupported job event {kind}")

"""Read-only agent execution projection for the v1.1 lookup endpoint.

The execution row and the command plan are the only sources of truth.  This
service never synthesizes an execution or a result; it only applies the
contract's role-based result redaction when projecting persisted state.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.core.errors import not_found
from app.core.security import Principal


class ExecutionService:
    def __init__(self, db) -> None:
        self._db = db

    async def get(self, principal: Principal, execution_id: str) -> dict[str, Any]:
        execution = await self._db.executions.get(execution_id)
        if execution is None:
            raise not_found("execution")
        command = await self._db.commands.get(execution["command_id"])
        if command is None:
            # The foreign key makes this unreachable in the authoritative schema,
            # but do not expose an incomplete projection if data is corrupt.
            raise not_found("execution")

        plan = self._plan(execution, command)
        steps = [self._step(index, step) for index, step in enumerate(plan)]
        result = execution.get("result")
        if not principal.is_viewer and isinstance(result, Mapping):
            visible_result: dict[str, Any] | None = dict(result)
        else:
            visible_result = None
        return {
            "execution": {
                "id": execution["id"],
                "command_id": execution["command_id"],
                "approval_id": execution.get("approval_id"),
                "status": execution["status"],
                "started_at": execution.get("started_at"),
                "finished_at": execution.get("finished_at"),
                "error_code": execution.get("error_code"),
                "error_message": execution.get("error_message"),
                "verification_id": execution.get("verification_id"),
                "created_at": execution["created_at"],
            },
            "steps": steps,
            "result": visible_result,
        }

    @staticmethod
    def _plan(execution: Mapping[str, Any], command: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = execution.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("plan"), list):
            return [dict(step) for step in result["plan"] if isinstance(step, Mapping)]
        raw_plan = command.get("plan")
        if isinstance(raw_plan, list):
            return [dict(step) for step in raw_plan if isinstance(step, Mapping)]
        return []

    @classmethod
    def _step(cls, index: int, step: Mapping[str, Any]) -> dict[str, Any]:
        raw_status = str(step.get("status") or "PENDING")
        # The v1.1 projection deliberately uses SUCCEEDED for the step-level
        # success vocabulary while the persisted command plan uses COMPLETED.
        status = {
            "COMPLETED": "SUCCEEDED",
            "WAITING_APPROVAL": "PENDING",
            "BLOCKED": "FAILED",
        }.get(raw_status, raw_status)
        raw_result = step.get("result")
        error: str | None = None
        if isinstance(raw_result, Mapping) and raw_result.get("error") is not None:
            error = str(raw_result["error"])
        output_summary: str | None = None
        if raw_result is not None:
            try:
                output_summary = json.dumps(raw_result, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                output_summary = str(raw_result)
            output_summary = output_summary[:500]
        return {
            "index": index,
            "tool": str(step.get("task") or "unknown"),
            "status": status,
            "error": error,
            "output_summary": output_summary,
        }

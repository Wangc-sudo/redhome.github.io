"""ops-web 数据新鲜度与知识索引新鲜度 API。"""

from datetime import datetime

from fastapi import Depends, HTTPException, Request

from common.public_data.dataset_contract import (
    CONTRACT_BY_KEY,
    get_contract,
    list_contracts,
)
from common.public_data.freshness import FreshnessChecker, evaluate_knowledge_sla
from common.public_data.freshness_repository import (
    FreshnessRepository,
    KnowledgeIndexFreshnessRepository,
)
from common.public_data.run_tracking import RunTrackingRepository
from common.public_data.ops_control import fetch_active_dead_letters


def _contract_out(contract):
    return {
        "dataset_key": contract.dataset_key,
        "version": contract.version,
        "kind": contract.kind,
        "owner": contract.owner,
        "source": contract.source,
        "schedule": contract.schedule,
        "expected_by": contract.expected_by.strftime("%H:%M"),
        "expected_day": contract.expected_day,
        "max_lag_minutes": contract.max_lag_minutes,
        "business_date_column": contract.business_date_column,
        "business_key_columns": contract.business_key_columns,
        "expected_scope": contract.expected_scope,
        "dq_rules": contract.dq_rules,
        "reconcile_rules": contract.reconcile_rules,
        "downstream_consumers": contract.downstream_consumers,
        "auto_retry": contract.auto_retry,
        "auto_catchup": contract.auto_catchup,
        "auto_replay": contract.auto_replay,
    }


def _dataset_actions(dataset_key):
    service_id = None
    if dataset_key in {"channel_daily_sales", "channel_monthly_target"}:
        service_id = "extract-channel"
    elif dataset_key == "wdt_order_line_fact":
        service_id = "extract-mart"
    return {
        "check": True,
        "run_service_id": service_id,
        "retry_service_id": service_id,
        "replay": dataset_key in {"channel_daily_sales", "wdt_order_line_fact"},
    }


def _knowledge_actions(index_key):
    service_id = {
        "kb_doc_bm25": "kb-sync-wiki",
        "kb_doc_vector": "rag-index-docs",
        "kb_product_structured": "kb-sync-products",
        "kb_product_text_vector": "rag-index-products",
        "kb_product_image_vector": "rag-index-products",
    }.get(index_key)
    return {"rebuild_service_id": service_id, "delete_plan": True}


def _unknown_item(contract):
    return {
        "dataset_key": contract.dataset_key,
        "status": "unknown",
        "error": "not_checked",
        "latest_business_date": None,
        "last_collected_at": None,
        "last_processed_at": None,
        "last_published_at": None,
        "lag_minutes": None,
        "source_target_diff": None,
        "downstream_consumers": contract.downstream_consumers,
        "last_run_id": None,
        "check_details": {},
        "checked_at": None,
    }


def _current_knowledge_item(item, contract):
    current = dict(item)
    status, error = evaluate_knowledge_sla(current, contract, datetime.utcnow())
    current["status"] = status
    current["last_error"] = error
    return current


def register_freshness_routes(
    app, *, connector, require_capability, manual_connector=None,
    business_require_capability=None, audit_writer=None,
):
    @app.get("/api/data-freshness", dependencies=[Depends(require_capability)])
    def list_data_freshness(status=None, source=None, owner=None, dataset=None):
        with connector() as conn:
            rows = FreshnessRepository(conn).list_all()
            dead_letters = fetch_active_dead_letters(conn)
        by_key = {row["dataset_key"]: row for row in rows}
        items = []
        contracts = list_contracts(kind="business") + list_contracts(kind="manual")
        for contract in contracts:
            if source and contract.source != source:
                continue
            if owner and contract.owner != owner:
                continue
            if dataset and dataset != contract.dataset_key:
                continue
            item = by_key.get(contract.dataset_key) or _unknown_item(contract)
            if status and item["status"] != status:
                continue
            item.update({"owner": contract.owner, "source": contract.source})
            items.append(item)
        severity = {"stale": 0, "blocked": 1, "degraded": 2, "unknown": 3, "fresh": 4}
        items.sort(key=lambda row: (severity.get(row["status"], 5), row["dataset_key"]))
        return {"items": items, "total": len(items), "dead_letters": dead_letters}

    @app.get("/api/data-freshness/{dataset_key}", dependencies=[Depends(require_capability)])
    def get_data_freshness(dataset_key: str):
        try:
            contract = get_contract(dataset_key)
        except KeyError:
            raise HTTPException(status_code=404, detail="not_found") from None
        if contract.kind == "knowledge":
            raise HTTPException(status_code=404, detail="not_found")
        with connector() as conn:
            item = FreshnessRepository(conn).get(dataset_key) or _unknown_item(contract)
            recent = RunTrackingRepository(conn).recent_runs(
                dataset_key=dataset_key, limit=10,
            )
        item.update({"owner": contract.owner, "source": contract.source})
        return {
            "item": item,
            "contract": _contract_out(contract),
            "recent_runs": recent,
            "actions": _dataset_actions(dataset_key),
        }

    @app.post("/api/data-freshness/{dataset_key}/check")
    def check_data_freshness(dataset_key: str,
                             viewer=Depends(require_capability)):
        try:
            contract = get_contract(dataset_key)
        except KeyError:
            raise HTTPException(status_code=404, detail="not_found") from None
        if contract.kind == "knowledge":
            raise HTTPException(status_code=400, detail="unsupported_dataset")
        with connector() as conn:
            manual_conn = None
            if manual_connector is not None and contract.kind == "manual":
                with manual_connector() as manual_conn:
                    result = FreshnessChecker(
                        connection=conn, manual_connection=manual_conn,
                    ).check(contract)
            else:
                result = FreshnessChecker(connection=conn).check(contract)
            FreshnessRepository(conn).upsert(result)
            if audit_writer is not None:
                actor = (
                    getattr(viewer, "userid", None)
                    or getattr(viewer, "user_id", None)
                    or "unknown"
                )
                audit_writer(conn, actor, dataset_key, result.status)
        return {
            "dataset_key": result.dataset_key,
            "status": result.status,
            "lag_minutes": result.lag_minutes,
            "error": result.error,
        }

    if business_require_capability is not None:
        @app.get("/api/business/{business_line}/data-freshness")
        def list_business_data_freshness(business_line: str, request: Request):
            business_require_capability(request, business_line)
            with connector() as conn:
                rows = FreshnessRepository(conn).list_all()
            by_key = {row["dataset_key"]: row for row in rows}
            items = []
            for contract in list_contracts(kind="business"):
                scope = contract.expected_scope or ""
                if scope.startswith("regions:"):
                    regions = scope.split(":", 1)[1].split(",")
                    if business_line not in regions:
                        continue
                current = by_key.get(contract.dataset_key) or _unknown_item(contract)
                items.append({
                    "dataset_key": contract.dataset_key,
                    "owner": contract.owner,
                    "status": current["status"],
                    "latest_business_date": current.get("latest_business_date"),
                    "expected_by": contract.expected_by.strftime("%H:%M"),
                    "max_lag_minutes": contract.max_lag_minutes,
                    "error": current.get("error"),
                })
            severity = {"stale": 0, "blocked": 1, "degraded": 2, "unknown": 3, "fresh": 4}
            items.sort(key=lambda row: (severity.get(row["status"], 3), row["dataset_key"]))
            return {"items": items, "total": len(items)}

    @app.get("/api/knowledge-indexes", dependencies=[Depends(require_capability)])
    def list_knowledge_indexes(source_type=None, status=None):
        with connector() as conn:
            rows = KnowledgeIndexFreshnessRepository(conn).list_all()
        by_key = {row["index_key"]: row for row in rows}
        items = []
        for contract in list_contracts(kind="knowledge"):
            item = by_key.get(contract.dataset_key) or {
                "index_key": contract.dataset_key,
                "source_type": contract.source,
                "source_id": "",
                "source_count": 0,
                "indexed_count": 0,
                "pending_count": 0,
                "failed_count": 0,
                "status": "unknown",
                "attempts": 0,
                "last_error": "not_checked",
            }
            item = _current_knowledge_item(item, contract)
            if source_type and item["source_type"] != source_type:
                continue
            if status and item["status"] != status:
                continue
            items.append(item)
        return {"items": items, "total": len(items)}

    @app.get(
        "/api/knowledge-indexes/{index_key}",
        dependencies=[Depends(require_capability)],
    )
    def get_knowledge_index(index_key: str):
        contract = CONTRACT_BY_KEY.get(index_key)
        if contract is None or contract.kind != "knowledge":
            raise HTTPException(status_code=404, detail="not_found")
        with connector() as conn:
            item = KnowledgeIndexFreshnessRepository(conn).get(index_key)
        if item is None:
            item = {
                "index_key": index_key,
                "source_type": contract.source,
                "source_id": "",
                "source_count": 0,
                "indexed_count": 0,
                "pending_count": 0,
                "failed_count": 0,
                "status": "unknown",
                "attempts": 0,
                "last_error": "not_checked",
            }
        item = _current_knowledge_item(item, contract)
        item["delete_plan_count"] = (
            int(item.get("pending_count") or 0)
            if item.get("last_error") in {"deletion_pending", "delete_threshold"}
            else 0
        )
        return {"item": item, "actions": _knowledge_actions(index_key)}

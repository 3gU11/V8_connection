from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime
from typing import Any

import pymysql
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


app = FastAPI(title="V7 Cloud Bridge", version="1.0.0")
BUILD_VERSION = "2026-05-23-v8-status-endpoint"


@app.middleware("http")
async def add_json_utf8_charset(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json") and "charset=" not in content_type.lower():
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


def _db_config() -> dict[str, Any]:
    address = os.getenv("MYSQL_ADDRESS", "").strip()
    host = os.getenv("MYSQL_HOST", "127.0.0.1").strip()
    port = int(os.getenv("MYSQL_PORT", "3306"))
    if address:
        if ":" in address:
            host_part, port_part = address.rsplit(":", 1)
            host = host_part.strip()
            port = int(port_part)
        else:
            host = address
    return {
        "host": host,
        "port": port,
        "user": os.getenv("MYSQL_USERNAME") or os.getenv("MYSQL_USER") or "root",
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DATABASE") or os.getenv("MYSQL_DB") or "rjfinshed",
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
    }


def get_conn():
    return pymysql.connect(**_db_config())


def require_v7_key(x_v7_api_key: str = Header(default="", alias="X-V7-API-KEY")) -> None:
    expected = os.getenv("V7_API_KEY", "")
    if not expected or not x_v7_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not hmac.compare_digest(x_v7_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Unauthorized")


def ensure_support_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS v7_idempotency_keys (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              idem_key VARCHAR(128) NOT NULL,
              method VARCHAR(16) NOT NULL,
              path VARCHAR(255) NOT NULL,
              response_json JSON NOT NULL,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              UNIQUE KEY uq_v7_idem (idem_key, method, path)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS v7_operation_logs (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              order_no VARCHAR(64) NOT NULL,
              operation VARCHAR(64) NOT NULL,
              operator VARCHAR(128) DEFAULT '',
              source_ip VARCHAR(64) DEFAULT '',
              old_status VARCHAR(32) DEFAULT '',
              new_status VARCHAR(32) DEFAULT '',
              idem_key VARCHAR(128) DEFAULT '',
              result VARCHAR(32) NOT NULL,
              detail TEXT,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              KEY idx_v7_operation_order (order_no),
              KEY idx_v7_operation_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute("SHOW COLUMNS FROM dealer_orders")
        existing = {row["Field"] for row in cur.fetchall()}
        for name, ddl in {
            "extra_remark": "ALTER TABLE dealer_orders ADD COLUMN extra_remark TEXT AFTER remark",
            "ERMQ": "ALTER TABLE dealer_orders ADD COLUMN ERMQ INT NOT NULL DEFAULT 0 AFTER extra_remark",
            "factory_pending": "ALTER TABLE dealer_orders ADD COLUMN factory_pending TINYINT(1) NOT NULL DEFAULT 0 AFTER ERMQ",
        }.items():
            if name not in existing:
                cur.execute(ddl)


def row_to_jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        else:
            out[key] = value
    return out


def fetch_order(conn, order_no: str, for_update: bool = False) -> list[dict[str, Any]]:
    suffix = " FOR UPDATE" if for_update else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM dealer_orders WHERE order_no=%s ORDER BY line_no, id{suffix}",
            (order_no,),
        )
        return list(cur.fetchall())


def summarize_order(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise HTTPException(status_code=404, detail="Order not found")
    base = row_to_jsonable(dict(rows[0]))
    items = [row_to_jsonable(dict(row)) for row in rows]
    base["items"] = items
    base["quantity"] = sum(int(row.get("quantity") or 0) for row in rows)
    base["approved_qty"] = sum(int(row.get("approved_qty") or 0) for row in rows)
    base["allocated_qty"] = sum(int(row.get("allocated_qty") or 0) for row in rows)
    statuses = {str(row.get("status") or "") for row in rows}
    if len(statuses) == 1:
        base["status"] = next(iter(statuses))
    elif "partial_allocated" in statuses or "allocated" in statuses:
        base["status"] = "partial_allocated"
    elif "contracted" in statuses:
        base["status"] = "contracted"
    elif "approved" in statuses:
        base["status"] = "approved"
    return base


def log_operation(
    conn,
    *,
    order_no: str,
    operation: str,
    operator: str,
    source_ip: str,
    old_status: str,
    new_status: str,
    idem_key: str,
    result: str,
    detail: str = "",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO v7_operation_logs
                (order_no, operation, operator, source_ip, old_status, new_status, idem_key, result, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (order_no, operation, operator, source_ip, old_status, new_status, idem_key, result, detail),
        )


def get_idempotent_response(conn, idem_key: str, method: str, path: str) -> dict[str, Any] | None:
    if not idem_key:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT response_json FROM v7_idempotency_keys WHERE idem_key=%s AND method=%s AND path=%s",
            (idem_key, method, path),
        )
        row = cur.fetchone()
        if not row:
            return None
        payload = row["response_json"]
        if isinstance(payload, str):
            return json.loads(payload)
        return payload


def save_idempotent_response(conn, idem_key: str, method: str, path: str, payload: dict[str, Any]) -> None:
    if not idem_key:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO v7_idempotency_keys (idem_key, method, path, response_json)
            VALUES (%s, %s, %s, CAST(%s AS JSON))
            ON DUPLICATE KEY UPDATE response_json=response_json
            """,
            (idem_key, method, path, json.dumps(payload, ensure_ascii=False)),
        )


# ---------------------------------------------------------------------------
# Status rank helper — higher rank = further along in the workflow
# ---------------------------------------------------------------------------
_STATUS_RANK: dict[str, int] = {
    "pending": 0,
    "approved": 1,
    "contracted": 2,
    "partial_allocated": 3,
    "allocated": 4,
    "completed": 5,
    "complete": 5,
    "rejected": 6,
    "cancelled": 7,
}


def _status_rank(status: str) -> int:
    return _STATUS_RANK.get(str(status or "").strip().lower(), -1)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class BatchSummaryRow(BaseModel):
    summary_id: str = ""
    batch_no: str
    expected_inbound_time: str | None = None
    model: str
    quantity: int = Field(ge=0)
    heightened: int = 0
    original_batch_no: str = ""
    original_expected_inbound_time: str | None = None
    updated_at: str | None = None


class BatchSummarySyncPayload(BaseModel):
    mode: str = "replace"
    rows: list[BatchSummaryRow]


class ReviewPayload(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    reviewedBy: str = ""
    reviewNote: str = ""
    updatedAt: str = ""
    factory_pending: int | None = None


class ContractPayload(BaseModel):
    contractNo: str
    v7OrderNo: str = ""
    contractedBy: str = ""


class AllocatePayload(BaseModel):
    contractNo: str = ""
    v7OrderNo: str = ""
    allocatedBy: str = ""


class CompletePayload(BaseModel):
    v7OrderNo: str = ""
    completedBy: str = ""


class AllocateLineItem(BaseModel):
    lineNo: int
    allocatedQty: int = Field(ge=0)


class AllocateLinesPayload(BaseModel):
    contractNo: str = ""
    v7OrderNo: str = ""
    allocatedBy: str = ""
    items: list[AllocateLineItem]


class V8StatusPayload(BaseModel):
    """
    Unified status-push payload from the V8 factory system (V7 local).
    Called via POST /api/dealer/orders/{order_no}/v8-status.

    A single endpoint replaces the need to call /review, /contract, /allocate,
    /complete separately — V8 decides the target status in one call.
    """
    status: str                          # approved | rejected | contracted | allocated | completed
    reviewedBy: str = ""                 # operator name
    reviewNote: str = ""                 # optional review note
    contractNo: str = ""                 # filled when status is contracted/allocated/completed
    v7OrderNo: str = ""                  # V7 internal sales order number
    updatedAt: str = ""                  # ISO datetime string from V7 (informational)
    factory_pending: int | None = None   # factory review pending flag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_wechat_batch_summary_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS wechat_batch_summary (
              summary_id CHAR(32) NOT NULL,
              batch_no VARCHAR(100) NOT NULL,
              expected_inbound_time DATETIME NULL,
              model VARCHAR(100) NOT NULL,
              quantity INT NOT NULL DEFAULT 0,
              heightened TINYINT(1) NOT NULL DEFAULT 0,
              original_batch_no VARCHAR(100) DEFAULT '',
              original_expected_inbound_time DATETIME NULL,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (summary_id),
              INDEX idx_wechat_batch_summary_batch (batch_no),
              INDEX idx_wechat_batch_summary_inbound (expected_inbound_time),
              INDEX idx_wechat_batch_summary_model (model)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute("SHOW COLUMNS FROM wechat_batch_summary")
        existing = {row["Field"] for row in cur.fetchall()}
        for name, ddl in {
            "heightened": "ALTER TABLE wechat_batch_summary ADD COLUMN heightened TINYINT(1) NOT NULL DEFAULT 0 AFTER quantity",
            "original_batch_no": "ALTER TABLE wechat_batch_summary ADD COLUMN original_batch_no VARCHAR(100) DEFAULT '' AFTER heightened",
            "original_expected_inbound_time": "ALTER TABLE wechat_batch_summary ADD COLUMN original_expected_inbound_time DATETIME NULL AFTER original_batch_no",
            "updated_at": "ALTER TABLE wechat_batch_summary ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP AFTER original_expected_inbound_time",
        }.items():
            if name not in existing:
                cur.execute(ddl)


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _empty_to_none(value: Any) -> Any:
    cleaned = _clean_text(value)
    return cleaned or None


def _batch_summary_id(row: BatchSummaryRow) -> str:
    supplied = _clean_text(row.summary_id)
    if supplied:
        return supplied[:32]
    raw = "|".join([
        _clean_text(row.batch_no),
        _clean_text(row.expected_inbound_time),
        _clean_text(row.model),
        str(int(row.heightened or 0)),
        _clean_text(row.original_batch_no),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True, "version": BUILD_VERSION}


@app.get("/api/v7/debug/db", dependencies=[Depends(require_v7_key)])
def debug_db():
    config = _db_config()
    masked_config = {
        "host": config["host"],
        "port": config["port"],
        "user": config["user"],
        "database": config["database"],
        "has_password": bool(config["password"]),
    }
    result: dict[str, Any] = {
        "version": BUILD_VERSION,
        "config": masked_config,
        "connected": False,
        "dealer_orders_exists": False,
    }
    with get_conn() as conn:
        result["connected"] = True
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE() AS db, CURRENT_USER() AS user")
            result["session"] = cur.fetchone()
            cur.execute("SHOW TABLES LIKE 'dealer_orders'")
            result["dealer_orders_exists"] = cur.fetchone() is not None
            if result["dealer_orders_exists"]:
                cur.execute("SHOW COLUMNS FROM dealer_orders")
                result["columns"] = [row["Field"] for row in cur.fetchall()]
                cur.execute("SELECT status, COUNT(*) AS count FROM dealer_orders GROUP BY status ORDER BY status")
                result["status_counts"] = list(cur.fetchall())
                cur.execute("SELECT COUNT(*) AS count FROM dealer_orders")
                result["total"] = cur.fetchone()
    return result


@app.post("/api/v7/wechat-batch-summary/sync", dependencies=[Depends(require_v7_key)])
def sync_wechat_batch_summary(payload: BatchSummarySyncPayload):
    if payload.mode != "replace":
        raise HTTPException(status_code=422, detail="Only replace mode is supported")
    with get_conn() as conn:
        ensure_wechat_batch_summary_table(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wechat_batch_summary")
            insert_sql = """
                INSERT INTO wechat_batch_summary
                  (summary_id, batch_no, expected_inbound_time, model, quantity,
                   heightened, original_batch_no, original_expected_inbound_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            for row in payload.rows:
                batch_no = _clean_text(row.batch_no)
                model = _clean_text(row.model)
                if not batch_no or not model:
                    continue
                cur.execute(
                    insert_sql,
                    (
                        _batch_summary_id(row),
                        batch_no,
                        _empty_to_none(row.expected_inbound_time),
                        model,
                        int(row.quantity or 0),
                        int(row.heightened or 0),
                        _clean_text(row.original_batch_no),
                        _empty_to_none(row.original_expected_inbound_time),
                    ),
                )
        conn.commit()
    return {"message": "ok", "rows": len(payload.rows)}


# Statuses that V7 may query via GET /api/v7/dealer-orders
_ALLOWED_LIST_STATUSES = {
    "pending", "approved", "contracted",
    "partial_allocated", "allocated",
    "completed", "complete",
    "rejected", "cancelled",
}


@app.get("/api/v7/dealer-orders", dependencies=[Depends(require_v7_key)])
def list_dealer_orders(status: str = "pending", page: int = 1, page_size: int = 100):
    normalized = status.strip().lower()
    if normalized not in _ALLOWED_LIST_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported status: {status!r}. Allowed: {sorted(_ALLOWED_LIST_STATUSES)}",
        )
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    offset = (page - 1) * page_size
    # Normalize "complete" alias to "completed" for DB query
    db_status = "completed" if normalized == "complete" else normalized
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_no
                FROM dealer_orders
                WHERE status=%s
                GROUP BY order_no
                ORDER BY MAX(created_at) DESC, order_no DESC
                LIMIT %s OFFSET %s
                """,
                (db_status, page_size, offset),
            )
            order_nos = [row["order_no"] for row in cur.fetchall()]
        data = [summarize_order(fetch_order(conn, order_no)) for order_no in order_nos]
        return {"data": data, "page": page, "page_size": page_size}


@app.post("/api/dealer/orders/{order_no}/v8-status", dependencies=[Depends(require_v7_key)])
def v8_push_status(
    order_no: str,
    payload: V8StatusPayload,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    """
    V8 (factory / V7 local) → Cloud status push.

    This unified endpoint replaces the pattern of calling /review, /contract,
    /allocate, /complete separately. V7's outbox calls this with the final
    desired status after any local operation.

    Status mapping:
      approved   → mark reviewed & approved (auto-fills approved_qty)
      rejected   → mark rejected
      contracted → auto-approve if pending, then mark contracted
      allocated  → auto-approve if pending, then mark allocated
      completed  → mark completed (regardless of intermediate steps)
    """
    target_status = _clean_text(payload.status).lower()
    valid_push_statuses = {"approved", "rejected", "contracted", "allocated", "completed", "complete"}
    if target_status not in valid_push_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported status for v8-status push: {target_status!r}. "
                   f"Allowed: {sorted(valid_push_statuses)}",
        )

    path = str(request.url.path)
    operator = _clean_text(payload.reviewedBy) or "v8-factory"
    note = _clean_text(payload.reviewNote)
    contract_no = _clean_text(payload.contractNo)
    v7_order_no = _clean_text(payload.v7OrderNo)

    with get_conn() as conn:
        ensure_support_tables(conn)

        # Idempotency: return cached response if same key already processed
        cached = get_idempotent_response(conn, idempotency_key, "POST", path)
        if cached:
            return cached

        rows = fetch_order(conn, order_no, for_update=True)
        if not rows:
            raise HTTPException(status_code=404, detail="Order not found")

        old_statuses = {str(row.get("status") or "") for row in rows}
        current_rank = max(_status_rank(s) for s in old_statuses)
        incoming_rank = _status_rank(target_status)

        # Never regress a status that is already ahead of the requested one
        if current_rank > incoming_rank:
            result = {
                "message": "skipped: local status is already ahead of requested",
                "local_status": max(old_statuses, key=_status_rank),
                "requested_status": target_status,
                "order": summarize_order(fetch_order(conn, order_no)),
            }
            save_idempotent_response(conn, idempotency_key, "POST", path, result)
            conn.commit()
            return result

        with conn.cursor() as cur:
            if target_status == "approved":
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='approved',
                        approved_qty=CASE WHEN approved_qty=0 THEN quantity ELSE approved_qty END,
                        reviewed_by=COALESCE(NULLIF(reviewed_by,''), %s),
                        reviewed_at=COALESCE(reviewed_at, NOW()),
                        review_note=CASE WHEN %s='' THEN review_note ELSE %s END,
                        factory_pending=COALESCE(%s, factory_pending)
                    WHERE order_no=%s AND status IN ('pending', 'approved')
                    """,
                    (operator, note, note, payload.factory_pending, order_no),
                )

            elif target_status == "rejected":
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='rejected',
                        reviewed_by=COALESCE(NULLIF(reviewed_by,''), %s),
                        reviewed_at=COALESCE(reviewed_at, NOW()),
                        review_note=CASE WHEN %s='' THEN review_note ELSE %s END
                    WHERE order_no=%s AND status NOT IN ('completed', 'complete', 'cancelled')
                    """,
                    (operator, note, note, order_no),
                )

            elif target_status == "contracted":
                # Step 1: auto-approve if still pending
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='approved',
                        approved_qty=CASE WHEN approved_qty=0 THEN quantity ELSE approved_qty END,
                        reviewed_by=COALESCE(NULLIF(reviewed_by,''), %s),
                        reviewed_at=COALESCE(reviewed_at, NOW())
                    WHERE order_no=%s AND status='pending'
                    """,
                    (operator, order_no),
                )
                # Step 2: mark contracted
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='contracted',
                        contract_no=CASE WHEN COALESCE(contract_no,'')='' THEN %s ELSE contract_no END,
                        v7_order_no=CASE WHEN COALESCE(v7_order_no,'')='' THEN %s ELSE v7_order_no END,
                        reviewed_by=COALESCE(NULLIF(reviewed_by,''), %s),
                        reviewed_at=COALESCE(reviewed_at, NOW())
                    WHERE order_no=%s AND status IN ('pending', 'approved')
                    """,
                    (contract_no, v7_order_no, operator, order_no),
                )

            elif target_status == "allocated":
                # Auto-approve if pending
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='approved',
                        approved_qty=CASE WHEN approved_qty=0 THEN quantity ELSE approved_qty END,
                        reviewed_by=COALESCE(NULLIF(reviewed_by,''), %s),
                        reviewed_at=COALESCE(reviewed_at, NOW())
                    WHERE order_no=%s AND status='pending'
                    """,
                    (operator, order_no),
                )
                # Mark allocated
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='allocated',
                        allocated_qty=quantity,
                        contract_no=CASE WHEN COALESCE(contract_no,'')='' THEN %s ELSE contract_no END,
                        v7_order_no=CASE WHEN COALESCE(v7_order_no,'')='' THEN %s ELSE v7_order_no END,
                        reviewed_by=COALESCE(NULLIF(reviewed_by,''), %s),
                        reviewed_at=COALESCE(reviewed_at, NOW())
                    WHERE order_no=%s AND status NOT IN ('completed', 'complete', 'cancelled')
                    """,
                    (contract_no, v7_order_no, operator, order_no),
                )

            elif target_status in {"completed", "complete"}:
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='completed',
                        allocated_qty=quantity,
                        v7_order_no=CASE WHEN COALESCE(v7_order_no,'')='' THEN %s ELSE v7_order_no END,
                        reviewed_by=COALESCE(NULLIF(reviewed_by,''), %s),
                        reviewed_at=COALESCE(reviewed_at, NOW())
                    WHERE order_no=%s AND status NOT IN ('cancelled')
                    """,
                    (v7_order_no, operator, order_no),
                )

        result = {
            "message": "ok",
            "requested_status": target_status,
            "order": summarize_order(fetch_order(conn, order_no)),
        }
        log_operation(
            conn,
            order_no=order_no,
            operation=f"v8-status:{target_status}",
            operator=operator,
            source_ip=request.client.host if request.client else "",
            old_status=",".join(sorted(old_statuses)),
            new_status=target_status,
            idem_key=idempotency_key,
            result="success",
            detail=f"contract_no={contract_no} v7_order_no={v7_order_no} note={note}",
        )
        save_idempotent_response(conn, idempotency_key, "POST", path, result)
        conn.commit()
        return result


@app.post("/api/v7/dealer-orders/{order_no}/review", dependencies=[Depends(require_v7_key)])
def review_order(
    order_no: str,
    payload: ReviewPayload,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    path = str(request.url.path)
    with get_conn() as conn:
        ensure_support_tables(conn)
        cached = get_idempotent_response(conn, idempotency_key, "POST", path)
        if cached:
            return cached
        rows = fetch_order(conn, order_no, for_update=True)
        old_statuses = {str(row.get("status") or "") for row in rows}
        if not rows:
            raise HTTPException(status_code=404, detail="Order not found")
        is_extra_review = payload.factory_pending is not None
        if old_statuses != {"pending"} and not is_extra_review:
            raise HTTPException(status_code=409, detail="Review only supports pending orders")
        new_status = payload.status
        with conn.cursor() as cur:
            if is_extra_review and new_status == "approved":
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET factory_pending=%s, reviewed_by=%s, reviewed_at=NOW(),
                        review_note=CASE WHEN %s='' THEN review_note ELSE %s END
                    WHERE order_no=%s AND status NOT IN ('completed', 'complete')
                    """,
                    (int(payload.factory_pending or 0), payload.reviewedBy, payload.reviewNote, payload.reviewNote, order_no),
                )
            elif new_status == "approved":
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='approved', approved_qty=quantity, reviewed_by=%s, reviewed_at=NOW(), review_note=%s,
                        factory_pending=COALESCE(%s, factory_pending)
                    WHERE order_no=%s AND status='pending'
                    """,
                    (payload.reviewedBy, payload.reviewNote, payload.factory_pending, order_no),
                )
            else:
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='rejected', reviewed_by=%s, reviewed_at=NOW(), review_note=%s,
                        factory_pending=COALESCE(%s, factory_pending)
                    WHERE order_no=%s AND (status='pending' OR %s IS NOT NULL)
                    """,
                    (payload.reviewedBy, payload.reviewNote, payload.factory_pending, order_no, payload.factory_pending),
                )
        result = {"message": "ok", "order": summarize_order(fetch_order(conn, order_no))}
        log_operation(
            conn,
            order_no=order_no,
            operation=f"review:{new_status}",
            operator=payload.reviewedBy,
            source_ip=request.client.host if request.client else "",
            old_status=",".join(sorted(old_statuses)),
            new_status=new_status,
            idem_key=idempotency_key,
            result="success",
            detail=payload.reviewNote,
        )
        save_idempotent_response(conn, idempotency_key, "POST", path, result)
        conn.commit()
        return result


@app.post("/api/v7/dealer-orders/{order_no}/contract", dependencies=[Depends(require_v7_key)])
def contract_order(
    order_no: str,
    payload: ContractPayload,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    path = str(request.url.path)
    with get_conn() as conn:
        ensure_support_tables(conn)
        cached = get_idempotent_response(conn, idempotency_key, "POST", path)
        if cached:
            return cached
        rows = fetch_order(conn, order_no, for_update=True)
        old_statuses = {str(row.get("status") or "") for row in rows}
        if not rows:
            raise HTTPException(status_code=404, detail="Order not found")
        if old_statuses != {"approved"}:
            raise HTTPException(status_code=409, detail="Contract only supports approved orders")
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dealer_orders
                SET status='contracted', contract_no=%s, v7_order_no=%s,
                    reviewed_by=CASE WHEN COALESCE(reviewed_by, '')='' THEN %s ELSE reviewed_by END,
                    reviewed_at=COALESCE(reviewed_at, NOW())
                WHERE order_no=%s AND status='approved'
                """,
                (payload.contractNo, payload.v7OrderNo, payload.contractedBy, order_no),
            )
        result = {"message": "ok", "order": summarize_order(fetch_order(conn, order_no))}
        log_operation(
            conn,
            order_no=order_no,
            operation="contract",
            operator=payload.contractedBy,
            source_ip=request.client.host if request.client else "",
            old_status=",".join(sorted(old_statuses)),
            new_status="contracted",
            idem_key=idempotency_key,
            result="success",
            detail=payload.contractNo,
        )
        save_idempotent_response(conn, idempotency_key, "POST", path, result)
        conn.commit()
        return result


@app.post("/api/v7/dealer-orders/{order_no}/allocate", dependencies=[Depends(require_v7_key)])
def allocate_order(
    order_no: str,
    payload: AllocatePayload,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    path = str(request.url.path)
    with get_conn() as conn:
        ensure_support_tables(conn)
        cached = get_idempotent_response(conn, idempotency_key, "POST", path)
        if cached:
            return cached
        rows = fetch_order(conn, order_no, for_update=True)
        old_statuses = {str(row.get("status") or "") for row in rows}
        if not rows:
            raise HTTPException(status_code=404, detail="Order not found")
        if not old_statuses.issubset({"approved", "contracted"}):
            raise HTTPException(status_code=409, detail="Allocate only supports approved or contracted orders")
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dealer_orders
                SET status='allocated', allocated_qty=quantity,
                    contract_no=CASE WHEN COALESCE(contract_no, '')='' THEN %s ELSE contract_no END,
                    v7_order_no=CASE WHEN COALESCE(v7_order_no, '')='' THEN %s ELSE v7_order_no END,
                    reviewed_by=CASE WHEN COALESCE(reviewed_by, '')='' THEN %s ELSE reviewed_by END,
                    reviewed_at=COALESCE(reviewed_at, NOW())
                WHERE order_no=%s AND status IN ('approved', 'contracted')
                """,
                (payload.contractNo, payload.v7OrderNo, payload.allocatedBy, order_no),
            )
        result = {"message": "ok", "order": summarize_order(fetch_order(conn, order_no))}
        log_operation(
            conn,
            order_no=order_no,
            operation="allocate",
            operator=payload.allocatedBy,
            source_ip=request.client.host if request.client else "",
            old_status=",".join(sorted(old_statuses)),
            new_status="allocated",
            idem_key=idempotency_key,
            result="success",
            detail=payload.v7OrderNo,
        )
        save_idempotent_response(conn, idempotency_key, "POST", path, result)
        conn.commit()
        return result


@app.post("/api/v7/dealer-orders/{order_no}/complete", dependencies=[Depends(require_v7_key)])
def complete_order(
    order_no: str,
    payload: CompletePayload,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    path = str(request.url.path)
    with get_conn() as conn:
        ensure_support_tables(conn)
        cached = get_idempotent_response(conn, idempotency_key, "POST", path)
        if cached:
            return cached
        rows = fetch_order(conn, order_no, for_update=True)
        old_statuses = {str(row.get("status") or "") for row in rows}
        if not rows:
            raise HTTPException(status_code=404, detail="Order not found")
        if not old_statuses.issubset({"approved", "contracted", "partial_allocated", "allocated", "completed"}):
            raise HTTPException(status_code=409, detail="Complete only supports approved, contracted, allocated or completed orders")
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dealer_orders
                SET status='completed', allocated_qty=quantity,
                    v7_order_no=CASE WHEN COALESCE(v7_order_no, '')='' THEN %s ELSE v7_order_no END,
                    reviewed_by=CASE WHEN COALESCE(reviewed_by, '')='' THEN %s ELSE reviewed_by END,
                    reviewed_at=COALESCE(reviewed_at, NOW())
                WHERE order_no=%s
                """,
                (payload.v7OrderNo, payload.completedBy, order_no),
            )
        result = {"message": "ok", "order": summarize_order(fetch_order(conn, order_no))}
        log_operation(
            conn,
            order_no=order_no,
            operation="complete",
            operator=payload.completedBy,
            source_ip=request.client.host if request.client else "",
            old_status=",".join(sorted(old_statuses)),
            new_status="completed",
            idem_key=idempotency_key,
            result="success",
            detail=payload.v7OrderNo,
        )
        save_idempotent_response(conn, idempotency_key, "POST", path, result)
        conn.commit()
        return result


@app.post("/api/v7/dealer-orders/{order_no}/allocate-lines", dependencies=[Depends(require_v7_key)])
def allocate_order_lines(
    order_no: str,
    payload: AllocateLinesPayload,
    request: Request,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    path = str(request.url.path)
    with get_conn() as conn:
        ensure_support_tables(conn)
        cached = get_idempotent_response(conn, idempotency_key, "POST", path)
        if cached:
            return cached
        rows = fetch_order(conn, order_no, for_update=True)
        old_statuses = {str(row.get("status") or "") for row in rows}
        if not rows:
            raise HTTPException(status_code=404, detail="Order not found")
        if not old_statuses.issubset({"approved", "contracted", "partial_allocated"}):
            raise HTTPException(status_code=409, detail="Line allocation only supports approved, contracted or partial_allocated orders")
        rows_by_line = {int(row["line_no"]): row for row in rows}
        for item in payload.items:
            row = rows_by_line.get(item.lineNo)
            if not row:
                raise HTTPException(status_code=422, detail=f"Unknown lineNo: {item.lineNo}")
            if item.allocatedQty > int(row.get("quantity") or 0):
                raise HTTPException(status_code=422, detail=f"allocatedQty exceeds quantity on lineNo {item.lineNo}")
        with conn.cursor() as cur:
            for item in payload.items:
                row = rows_by_line[item.lineNo]
                line_status = (
                    "allocated" if item.allocatedQty >= int(row.get("quantity") or 0)
                    else ("partial_allocated" if item.allocatedQty > 0 else row.get("status"))
                )
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET allocated_qty=%s, status=%s,
                        contract_no=CASE WHEN COALESCE(contract_no, '')='' THEN %s ELSE contract_no END,
                        v7_order_no=CASE WHEN COALESCE(v7_order_no, '')='' THEN %s ELSE v7_order_no END
                    WHERE order_no=%s AND line_no=%s
                    """,
                    (item.allocatedQty, line_status, payload.contractNo, payload.v7OrderNo, order_no, item.lineNo),
                )
            updated = fetch_order(conn, order_no, for_update=True)
            if all(int(row.get("allocated_qty") or 0) >= int(row.get("quantity") or 0) for row in updated):
                order_status = "allocated"
            elif any(int(row.get("allocated_qty") or 0) > 0 for row in updated):
                order_status = "partial_allocated"
            else:
                order_status = "contracted" if "contracted" in old_statuses else "approved"
            cur.execute("UPDATE dealer_orders SET status=%s WHERE order_no=%s", (order_status, order_no))
        result = {"message": "ok", "order": summarize_order(fetch_order(conn, order_no))}
        log_operation(
            conn,
            order_no=order_no,
            operation="allocate-lines",
            operator=payload.allocatedBy,
            source_ip=request.client.host if request.client else "",
            old_status=",".join(sorted(old_statuses)),
            new_status=result["order"]["status"],
            idem_key=idempotency_key,
            result="success",
            detail=json.dumps([item.model_dump() for item in payload.items], ensure_ascii=False),
        )
        save_idempotent_response(conn, idempotency_key, "POST", path, result)
        conn.commit()
        return result

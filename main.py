from __future__ import annotations

import hmac
import json
import os
from datetime import datetime
from typing import Any

import pymysql
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


app = FastAPI(title="V7 Cloud Bridge", version="1.0.0")
BUILD_VERSION = "2026-05-19-c50536b-debug-db"


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


class ReviewPayload(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    reviewedBy: str = ""
    reviewNote: str = ""


class ContractPayload(BaseModel):
    contractNo: str
    v7OrderNo: str = ""
    contractedBy: str = ""


class AllocatePayload(BaseModel):
    contractNo: str = ""
    v7OrderNo: str = ""
    allocatedBy: str = ""


class AllocateLineItem(BaseModel):
    lineNo: int
    allocatedQty: int = Field(ge=0)


class AllocateLinesPayload(BaseModel):
    contractNo: str = ""
    v7OrderNo: str = ""
    allocatedBy: str = ""
    items: list[AllocateLineItem]


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


@app.get("/api/v7/dealer-orders", dependencies=[Depends(require_v7_key)])
def list_dealer_orders(status: str = "pending", page: int = 1, page_size: int = 100):
    allowed_statuses = {"pending", "approved", "contracted", "partial_allocated", "allocated", "completed"}
    if status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Unsupported status")
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    offset = (page - 1) * page_size
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
                (status, page_size, offset),
            )
            order_nos = [row["order_no"] for row in cur.fetchall()]
        data = [summarize_order(fetch_order(conn, order_no)) for order_no in order_nos]
        return {"data": data, "page": page, "page_size": page_size}


@app.post("/api/v7/dealer-orders/{order_no}/review", dependencies=[Depends(require_v7_key)])
def review_order(order_no: str, payload: ReviewPayload, request: Request, idempotency_key: str = Header(default="", alias="Idempotency-Key")):
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
        if old_statuses != {"pending"}:
            raise HTTPException(status_code=409, detail="Review only supports pending orders")
        new_status = payload.status
        with conn.cursor() as cur:
            if new_status == "approved":
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='approved', approved_qty=quantity, reviewed_by=%s, reviewed_at=NOW(), review_note=%s
                    WHERE order_no=%s AND status='pending'
                    """,
                    (payload.reviewedBy, payload.reviewNote, order_no),
                )
            else:
                cur.execute(
                    """
                    UPDATE dealer_orders
                    SET status='rejected', reviewed_by=%s, reviewed_at=NOW(), review_note=%s
                    WHERE order_no=%s AND status='pending'
                    """,
                    (payload.reviewedBy, payload.reviewNote, order_no),
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
def contract_order(order_no: str, payload: ContractPayload, request: Request, idempotency_key: str = Header(default="", alias="Idempotency-Key")):
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
def allocate_order(order_no: str, payload: AllocatePayload, request: Request, idempotency_key: str = Header(default="", alias="Idempotency-Key")):
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


@app.post("/api/v7/dealer-orders/{order_no}/allocate-lines", dependencies=[Depends(require_v7_key)])
def allocate_order_lines(order_no: str, payload: AllocateLinesPayload, request: Request, idempotency_key: str = Header(default="", alias="Idempotency-Key")):
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
                line_status = "allocated" if item.allocatedQty >= int(row.get("quantity") or 0) else ("partial_allocated" if item.allocatedQty > 0 else row.get("status"))
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


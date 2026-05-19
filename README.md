# V7 Cloud Bridge

这是部署在微信云托管上的 V7 专用对接层。它只访问云端 MySQL，不连接 V7 本地数据库。

## 连接边界

```text
小程序 -> 云托管业务接口 -> 云端 MySQL
V7 本地 -> 本服务 /api/v7/* -> 云端 MySQL
```

V7 本地必须主动调用本服务；云端不要主动访问 V7 本地库。

## 环境变量

```env
MYSQL_ADDRESS=10.4.105.5:3306
MYSQL_USERNAME=v7_bridge_user
MYSQL_PASSWORD=实际密码
MYSQL_DATABASE=rjfinshed
V7_API_KEY=32位以上随机密钥
PORT=80
```

上线前必须创建低权限 MySQL 用户，禁止使用 `root`。该用户至少需要访问 `dealer_orders`，以及自动创建/写入 `v7_idempotency_keys`、`v7_operation_logs`。

## API

所有接口必须带：

```http
X-V7-API-KEY: <V7_API_KEY>
```

写接口建议带：

```http
Idempotency-Key: <uuid>
```

### 拉取订单

```http
GET /api/v7/dealer-orders?status=pending
```

默认用于 V7 拉取等待审核的订单。

### 审核通过/驳回

```http
POST /api/v7/dealer-orders/{orderNo}/review

{
  "status": "approved",
  "reviewedBy": "V7管理员",
  "reviewNote": "审核通过"
}
```

`status` 也可以是 `rejected`。只允许从 `pending` 更新。

### 合同写回

```http
POST /api/v7/dealer-orders/{orderNo}/contract

{
  "contractNo": "HT20260519001",
  "v7OrderNo": "SO20260519001",
  "contractedBy": "V7管理员"
}
```

只允许从 `approved` 更新到 `contracted`。

### 整单配货

```http
POST /api/v7/dealer-orders/{orderNo}/allocate

{
  "contractNo": "HT20260519001",
  "v7OrderNo": "SO20260519001",
  "allocatedBy": "V7管理员"
}
```

只允许从 `approved` 或 `contracted` 更新到 `allocated`。

### 行级部分配货

```http
POST /api/v7/dealer-orders/{orderNo}/allocate-lines

{
  "contractNo": "HT20260519001",
  "v7OrderNo": "SO20260519001",
  "allocatedBy": "V7管理员",
  "items": [
    { "lineNo": 1, "allocatedQty": 1 }
  ]
}
```

行级配货数量不能超过订单行数量。汇总后自动得到 `partial_allocated` 或 `allocated`。

## 本地调试

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

健康检查：

```http
GET /health
```
"# V8_connection" 

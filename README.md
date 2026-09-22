# 字幕时间轴修复 API（Subtitle Timeline Normalizer）

基于 **Python + FastAPI + Pydantic** 的微服务，修复顺序固定的字幕片段时间轴：
在**保持文本、时长和锁定片段位置不变**的前提下，消除重叠并保证最小间隔，
使所有片段的**总绝对位移最小**。约束无解时返回具体冲突片段与原因。

- 服务地址：<http://localhost:8000>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

## 一条命令启动

```bash
docker compose up --build
```

启动后访问 <http://localhost:8000>，健康检查见 <http://localhost:8000/health>。

> 也可以不用 Compose：
>
> ```bash
> docker build -t subtitle-normalizer .
> docker run --rm -p 8000:8000 subtitle-normalizer
> ```

镜像构建时会自动安装 `requirements.txt` 中的全部依赖（fastapi、uvicorn、pydantic），
无需在宿主机上安装任何 Python 包。

## 本地开发（不使用 Docker）

```bash
pip install -r requirements-dev.txt   # 含 pytest / httpx
uvicorn app.main:app --reload --port 8000
pytest -q                              # 运行测试
```

## 配置

无需任何必填配置。唯一可调整的运行参数是端口：

| 配置项 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 宿主机端口映射 | `docker-compose.yml` 的 `ports` | `8000:8000` | 如需改用 8080，改为 `"8080:8000"` |
| 容器监听端口 | Dockerfile `CMD` / 环境变量 `UVICORN_PORT` | `8000` | 健康检查读取该变量 |
| `min_gap_ms` | 每个请求的 JSON 字段 | `0` | 相邻片段之间要求的最小间隔（毫秒） |

## API

### `GET /health`

健康检查，返回 `200`：

```json
{ "status": "ok", "version": "1.0.0" }
```

### `POST /normalize`

请求体：

| 字段 | 类型 | 必填 | 约束 / 说明 |
| --- | --- | --- | --- |
| `segments` | `array` | 是（可为空数组） | 片段按**播放顺序**给出，顺序永不改变 |
| `segments[].id` | `string \| null` | 否 | 调用方自定义 ID，原样回显 |
| `segments[].text` | `string` | 是 | 字幕文本，**保持不变** |
| `segments[].start_ms` | `integer` | 是 | 起始时间（毫秒），`>= 0` |
| `segments[].end_ms` | `integer` | 是 | 结束时间（毫秒，左闭右开），必须 `> start_ms` |
| `segments[].pinned` | `boolean` | 否 | 为 `true` 时该片段**不可移动**（锁定在 `start_ms`） |
| `min_gap_ms` | `integer` | 否 | 相邻片段最小间隔（毫秒），`>= 0`，默认 `0` |

额外的未知字段会被忽略，方便直接传入完整字幕记录。

#### 成功响应（200）

```bash
curl -s -X POST http://localhost:8000/normalize \
  -H 'Content-Type: application/json' \
  -d '{
    "min_gap_ms": 50,
    "segments": [
      {"id": "a", "text": "你好", "start_ms": 0,    "end_ms": 600},
      {"id": "b", "text": "世界", "start_ms": 800,  "end_ms": 1800, "pinned": true},
      {"id": "c", "text": "再见", "start_ms": 1500, "end_ms": 2200}
    ]
  }'
```

```json
{
  "segments": [
    {"index": 0, "id": "a", "text": "你好", "start_ms": 0,    "end_ms": 600,  "pinned": false, "displacement_ms": 0},
    {"index": 1, "id": "b", "text": "世界", "start_ms": 800,  "end_ms": 1800, "pinned": true,  "displacement_ms": 0},
    {"index": 2, "id": "c", "text": "再见", "start_ms": 1850, "end_ms": 2550, "pinned": false, "displacement_ms": 350}
  ],
  "min_gap_ms": 50,
  "total_absolute_displacement_ms": 350
}
```

返回保证：

1. 每条片段 `text` 与时长 `end_ms - start_ms` 与输入完全一致；
2. `pinned: true` 的片段起止时间逐毫秒不变；
3. 相邻片段满足 `start[i+1] >= end[i] + min_gap_ms`，且 `start[0] >= 0`；
4. `total_absolute_displacement_ms = Σ |new_start - old_start|` 在所有可行时间轴中**最小**。

#### 约束无解（409）

当锁定片段之间互相矛盾（例如两个锁定片段必然重叠，或锁定位置之前无法塞下前面的
全部片段）时返回 `409 Conflict`，`conflicts` 列出每一对冲突片段、原因代码、
人可读说明，以及所需时间与缺口：

```bash
curl -s -X POST http://localhost:8000/normalize \
  -H 'Content-Type: application/json' \
  -d '{"segments": [
    {"id": "a", "text": "x", "start_ms": 0,   "end_ms": 1000, "pinned": true},
    {"id": "b", "text": "y", "start_ms": 500, "end_ms": 1500, "pinned": true}
  ]}'
```

```json
{
  "error": "infeasible_constraints",
  "message": "The locked segments cannot be satisfied simultaneously; see conflicts for details.",
  "conflicts": [
    {
      "from_index": 0,
      "from_id": "a",
      "to_index": 1,
      "to_id": "b",
      "reason_code": "locked_segments_overlap_or_gap_violation",
      "reason": "Locked segments 0 and 1 cannot both keep their positions: 1000 ms are required between their starts but only 500 ms are available (shortfall 500 ms, minimum gap 0 ms)",
      "required_start_ms": 1000,
      "actual_start_ms": 500,
      "shortfall_ms": 500
    }
  ]
}
```

`from_index` 为 `null` 表示冲突的另一端是时间轴起点（0 ms 边界），
此时 `reason_code` 为 `locked_before_timeline_start`。`index` 均为输入数组的
**0 基下标**。

#### 参数错误（422）

`end_ms <= start_ms`、`start_ms < 0`、`min_gap_ms < 0` 等返回 FastAPI 标准
`422 Unprocessable Entity` 校验错误。

## 算法说明

令片段 `i` 的时长为 `d_i`，定义累计常量 `c_0 = 0`、
`c_i = Σ_{j<i}(d_j + min_gap_ms)`，并做变量替换：

```
y_i = x_i - c_i,   t_i = a_i - c_i
```

所有顺序约束随即塌缩为单调性约束 `y_0 ≥ 0`、`y_{i+1} ≥ y_i`，目标函数变为
`Σ |y_i - t_i|`。这是带固定观测（pinned）点的 **L1 保序回归**，使用
**PAVA（Pool Adjacent Violators Algorithm）+ 分块加权中位数**求解，
全程整数运算，结果精确且确定（中位数取下中位数，等价最优解中倾向整体靠左）。
锁定点之间的可行性在求解前单独检查，任意一对锚点矛盾都会在 409 响应中给出。

## 项目结构

```
app/
  main.py       # FastAPI 路由：/、/health、POST /normalize
  schemas.py    # Pydantic v2 请求/响应模型与输入校验
  solver.py     # 核心求解器（可行性检查 + L1 保序回归 PAVA）
  tests/        # pytest 单元/接口测试（含暴力 DP 最优性随机校验）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

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

### `POST /normalize-document`

直接接收整份 **SRT** 或 **WebVTT** 文档，按文档中的 cue 顺序调用同一个时间轴求解器，
返回同格式的 `normalized_content` 与总位移。

**只修改 cue 时间行上的两个时间戳**，其余内容与顺序原样保留：

- SRT：编号（即使不连续/补零）、一行或多行 cue 文本；
- WebVTT：`WEBVTT` 文件头及其标题、`NOTE`/`STYLE`/`REGION` 块、cue 标识行、
  时间行末尾的 settings（如 `align:start position:20%`）；
- 两种格式的换行风格（`\n` / `\r\n`）、行首缩进、时间戳分隔符（`,`/`.`）均保持一致。

请求体：

| 字段 | 类型 | 必填 | 约束 / 说明 |
| --- | --- | --- | --- |
| `format` | `string` | 是 | `"srt"` 或 `"webvtt"` |
| `content` | `string` | 是 | 原始文档全文（允许空 SRT 或仅有头部的 WebVTT） |
| `min_gap_ms` | `integer` | 是 | 相邻 cue 最小间隔（毫秒），`>= 0` |
| `pinned_indices` | `array<integer>` | 否 | 需要锁定的 cue 的 **0 基下标**（按文档中 cue 的出现顺序，头部与 `NOTE`/`STYLE`/`REGION` 块不计号），不可重复、不可为负 |

#### 成功响应（200）

SRT 示例（命令行里用文件传入可避免手工转义）：

```bash
cat > subtitles.srt <<'EOF'
1
00:00:00,000 --> 00:00:01,000
Hello
world

2
00:00:00,500 --> 00:00:02,000
Middle

3
00:00:00,600 --> 00:00:03,000
Bye
EOF

curl -s -X POST http://localhost:8000/normalize-document \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"format":"srt","content":open("subtitles.srt").read(),"min_gap_ms":0}))')"
```

```json
{
  "format": "srt",
  "normalized_content": "1\n00:00:00,000 --> 00:00:01,000\nHello\nworld\n\n2\n00:00:01,000 --> 00:00:02,500\nMiddle\n\n3\n00:00:02,500 --> 00:00:04,900\nBye\n",
  "cue_count": 3,
  "min_gap_ms": 0,
  "total_absolute_displacement_ms": 2400
}
```

编号与多行文本保持不变，只有两个重叠 cue 的时间戳被推后。把第 1 条 cue 锁定只需
加 `"pinned_indices": [0]`（锁定位置必须容得下其前后所有 cue，否则返回 409）。

WebVTT 示例（文件头、`NOTE`/`REGION`、cue 标识、settings 全部原样保留）：

```bash
curl -s -X POST http://localhost:8000/normalize-document \
  -H 'Content-Type: application/json' \
  -d '{
    "format": "webvtt",
    "min_gap_ms": 0,
    "content": "WEBVTT - demo\n\nNOTE keep me\n\nREGION\nid:top\n\ncue-a\n00:00.000 --> 00:01.000 line:0\nHello\n\n00:00.500 --> 00:02.000\nMiddle\n"
  }'
```

```json
{
  "format": "webvtt",
  "normalized_content": "WEBVTT - demo\n\nNOTE keep me\n\nREGION\nid:top\n\ncue-a\n00:00.000 --> 00:01.000 line:0\nHello\n\n00:01.000 --> 00:02.500\nMiddle\n",
  "cue_count": 2,
  "min_gap_ms": 0,
  "total_absolute_displacement_ms": 500
}
```

> 时间戳按原文风格写回（SRT 长格式 `HH:MM:SS,mmm`，WebVTT 长格式或短格式
> `MM:SS.mmm`）；若短格式时间戳被推移到 1 小时之后，会自动提升为长格式以保证合法。

#### 语法错误（422）

文档不符合 SRT/WebVTT 语法（缺少 `WEBVTT` 头、时间行无法解析、缺少编号行等）时
返回 `422`，`line` 为**从 1 开始、包含空行与文件头**的行号：

```json
{
  "error": "document_syntax_error",
  "format": "srt",
  "line": 6,
  "message": "expected the SRT timing line ('-->') after the cue number"
}
```

`pinned_indices` 超出 cue 数量时同样返回 `422`
（`error` 为 `invalid_pinned_indices`，并给出越界下标与 `cue_count`）；
`format` 取值非法、`min_gap_ms < 0`、下标为负或重复则返回 FastAPI 标准校验错误。

#### 求解冲突（409）

锁定 cue（`pinned_indices`）之间无法同时满足时，沿用 `/normalize` 的
`409 Conflict`，`conflicts[].from_index` / `to_index` 同样是 cue 的 0 基下标。

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
  main.py       # FastAPI 路由：/、/health、POST /normalize、POST /normalize-document
  schemas.py    # Pydantic v2 请求/响应模型与输入校验
  solver.py     # 核心求解器（可行性检查 + L1 保序回归 PAVA）
  documents.py  # SRT / WebVTT 解析与只改时间戳的无损重建
  tests/        # pytest 单元/接口测试（含暴力 DP 最优性随机校验）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

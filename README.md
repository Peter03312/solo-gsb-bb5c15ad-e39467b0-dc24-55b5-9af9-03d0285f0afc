# 字幕协校（自托管）

一个零第三方依赖的字幕协作校对网页应用：Python 标准库 + SQLite + 浏览器页面。
上传 UTF-8 的 SRT/VTT 后解析为带稳定 ID 的字幕条，多人可同时在线改文字、改时间、
插入/删除字幕条；每次保存都是一次带版本号的三方合并，冲突绝不覆盖，整轨校验不过则整批拒绝。

## 快速开始

### Docker Compose（推荐，一键运行）

```bash
docker compose up --build
```

### 或只用 Docker

```bash
docker build -t subtitle-coop .
docker run -d -p 8000:8000 -v subtitle-data:/data subtitle-coop
```

容器启动时自动初始化数据库（建库建表），数据持久化在 `subtitle-data` 卷。

### 本地运行（无需 Docker，Python ≥ 3.8）

```bash
python app.py
```

### 访问地址

浏览器打开 **http://localhost:8000**

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8000` | 网页监听端口（Compose 中映射到宿主机 8000） |
| `HOST` | `0.0.0.0` | 监听地址 |
| `DB_PATH` | 本地 `./data/subtitles.db`，容器内 `/data/subtitles.db` | SQLite 数据库路径，启动时自动创建并建表 |

## 使用流程

1. 首页选择 `.srt` / `.vtt` 文件上传，解析成功即创建项目并生成 v1；
   **格式错误（非 UTF-8、时间戳非法、缺 `-->`、终点不早于起点等）直接拒收，不创建项目**。
2. 编辑页修改文字/时间、删除或在任意位置插入字幕条，点「保存（提交合并）」。
3. 「历史版本」页可查看每个版本快照，并按指定版本导出 SRT 或 VTT。

## 合并与冲突规则

- 每次提交都携带编辑时的版本号（`base_version`）。
- 期间**别人只改了你没动的字幕条** → 自动三方合并，生成新版本。
- **同一条字幕被双方修改、或一方改一方删、或你的插入锚点被对方删** → 返回 409
  及该条的「你编辑时的版本 / 对方当前版本 / 你的修改」三方状态，页面要求逐条人工
  选择保留哪一方后重新提交，**绝不静默覆盖**。
- 双方改成完全一样的内容不算冲突。

## 整轨校验（每次合并后执行）

合并结果须满足：所有时间非负、每条起点早于终点、任意两条字幕时间不重叠。
不满足则**整批拒绝**（不落库），并逐条说明原因（哪一条、什么问题）。

## 项目结构

```
app.py        HTTP 服务与页面（仅标准库 http.server）
subtitle.py   SRT/VTT 解析、序列化、整轨校验
store.py      SQLite 存储：项目 / 版本快照 / 三方合并
test_core.py  单元测试：python -m unittest -v
Dockerfile / docker-compose.yml
```

## 运行测试

```bash
python -m unittest -v
```

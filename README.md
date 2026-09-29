# WebP 派生图服务

上传 PNG/JPEG（≤ 4 MiB），在页面上框选裁切区域，由后台 worker 生成 **256 px** 与
**512 px** 两种 WebP 派生图并原子发布。系统由 React 页面、FastAPI API、SQLite
持久化作业队列和独立 worker 组成，全部通过 Docker Compose 运行。

## 快速开始

```bash
docker compose build
docker compose up        # 页面: http://localhost:8080
```

固定验收（依次执行）：

```bash
docker compose config --quiet
docker compose build
docker compose run --rm verify
```

`verify` 是一次性测试服务：对真实的 API + worker 运行端到端测试，全部通过则
以退出码 0 结束。

## 服务组成

| 服务     | 说明 |
| -------- | ---- |
| `web`    | nginx 提供 React 静态页面，并反向代理 `/api`、`/media` 到 API（端口 8080） |
| `api`    | FastAPI：上传、裁切作业管理、发布版本读取、媒体文件服务（内部端口 8000） |
| `worker` | 独立进程：认领作业、渲染派生图、原子发布；与 API 共用同一个镜像 |
| `verify` | 一次性 pytest 套件（见下），挂载 docker socket 用于重启恢复测试 |

API、worker 与 verify 共享命名卷 `data`：SQLite 库（`app.db`，WAL 模式）、
`media/{originals,derivatives,tmp}` 以及测试钩子目录 `flags/`。

## 核心语义

- **去重复用**：原图按 SHA-256 去重；`(图片, 裁切框)` 相同的申请复用同一作业——
  已完成则直接返回既有派生结果，进行中则返回在途作业；裁切参数改变才产生
  **新版本**（版本号单调递增）。已失败/已取消的配方再次申请会以新版本重新排队。
- **只展示已发布版本**：页面只渲染 `GET /api/images/{id}` 中服务端确认的
  `published` 字段。发布指针 `published_version` 只能向更大版本单调前进，
  且与作业置为 `succeeded` 在**同一个事务**内提交——因此迟到完成的旧作业
  永远无法覆盖较新的当前选择。
- **原子发布**：worker 先把两个尺寸写入 `media/tmp/`，两个都成功后才
  `rename` 进 `media/derivatives/`，再一次性提交数据库事务。任何中途崩溃
  都不会产生"只发布了一个尺寸"的半成品。
- **取消与完成只有一个终态**：所有状态迁移都是条件更新
  （`UPDATE ... WHERE status IN ('queued','processing')`）。取消与完成竞争时，
  先提交者获胜；输掉竞争的 worker 会丢弃已生成的文件，取消返回 409 表示
  作业已进入终态。
- **崩溃恢复**：worker 启动时清空 `media/tmp/` 并把崩溃时处于 `processing`
  的作业重新排队（`attempts` 递增），未完成的作业因此自动重试。

## API 摘要

| 方法/路径 | 说明 |
| --------- | ---- |
| `POST /api/images` | multipart 上传；415 非法类型 / 413 超过 4 MiB / 400 无法解码；相同内容幂等复用 |
| `GET /api/images/{id}` | 图片信息 + 当前已发布版本（含派生图 URL） |
| `POST /api/images/{id}/jobs` | 申请裁切派生 `{x,y,w,h}`；422 裁切越界；相同配方复用（`reused: true`） |
| `GET /api/images/{id}/jobs` | 该图片的作业列表 |
| `GET /api/jobs/{id}` | 单个作业状态 |
| `POST /api/jobs/{id}/cancel` | 取消；已进入终态返回 409 |
| `GET /api/health` | 健康检查 |
| `GET /media/{originals\|derivatives}/{file}` | 媒体文件 |

派生图规则：裁切区域等比缩放至最长边 = 256 / 512 px，WebP（quality 90）。

## 测试（verify 服务）

`verify/test_verify.py` 覆盖需求要求的全部场景：

1. **重复申请**：相同摘要 + 相同裁切复用已完成结果（同作业、同版本）；
   在途作业同样去重；参数改变产生新版本；相同文件重复上传复用图片。
2. **坏图**：垃圾字节、截断 PNG → 400；错误 Content-Type、GIF → 415；
   超过 4 MiB → 413。
3. **裁切越界**：负坐标、零/负尺寸、超出宽高边界等 → 422；整幅与边缘
   1 px 裁切合法。
4. **取消竞态**：排队中取消后永不被执行；处理中取消后 worker 迟到的发布
   被丢弃且文件被清理；已完成作业取消返回 409；连续创建+立即取消的压力
   循环中每个作业都收敛到唯一稳定终态。
5. **重启恢复**：通过 docker socket 在作业处理中途硬重启 worker 容器，
   验证启动时清理临时文件（含预置的垃圾文件）并重试未完成作业直至成功。
6. **旧响应回写**：用测试钩子让旧版本作业晚于新版本完成，验证发布指针
   不被回写、页面可见的始终是较新版本。

### 测试钩子（`$DATA_DIR/flags/`）

worker 识别以下标志文件，供 verify 构造确定性的竞态时序：

- `skip_all`：存在时不认领任何作业；
- `skip_job_<id>`：存在时不认领指定作业；
- `pause_mid_all`：存在时在写完第一个临时派生文件后暂停。

## 本地开发（无 Docker）

```bash
pip install -r api/requirements.txt
DATA_DIR=/tmp/data uvicorn app.main:app --port 8000   # 于 api/ 目录
DATA_DIR=/tmp/data python -m app.worker               # 另一个终端
cd web && npm install && npm run dev                  # 页面: http://localhost:5173
cd verify && API_URL=http://localhost:8000 DATA_DIR=/tmp/data pytest
# （无 docker socket 时重启恢复用例自动跳过）
```

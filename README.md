# 1688 图搜比价

Contract v2 双模式插件（FastAPI）：读取本地图片文件夹，在 1688 以图搜图，生成含价格最低 Top3 店铺的结果 Excel。

## 业务约定

| 项 | 说明 |
| --- | --- |
| 入口 | 本地 **图片文件夹**（如 `E:\图片`），支持 jpg/png/webp/gif/bmp |
| 图搜 | 每张图上传到 1688 以图搜图（必须跳转结果页才解析） |
| 结果 | 相似度锚定后最低价 Top3：链接 / 名称 / 价格 / 店铺 / 备注 |
| 输出 | 自定义 `outputExcel`（完整 xlsx 或目录）；留空则在图片目录下自动生成 |
| 大批量 | 每张结果先写入 SQLite 断点库；服务重启后继续，全部处理完再流式导出完整 Excel |
| 处理单位 | 每个输入文件独立上传图搜；即使内容相同也不复用其他文件的结果 |
| 安全节奏 | 使用内置固定节奏：每张请求间隔 2–4 秒，每 50 张冷却 45–75 秒，每 500 张休息 2–3 分钟；页面无需填写参数 |
| 每日上限 | 插件不设置每日图搜请求数量上限；若平台要求验证，任务会暂停并等待人工处理 |
| 验证处理 | 出现登录或验证要求时，任务暂停。用户在**可见浏览器**中自行完成后，再在控制台点击「人工处理后继续」 |

## 本地开发

```powershell
git clone <你的私有仓库地址>
cd 1688chajian
uv sync
uv run playwright install chromium
Copy-Item .env.example .env
```

### 长驻冒烟

```powershell
'{}' | uv run python run.py --plugin-id 90099 --port 18767
```

浏览器打开 `http://127.0.0.1:18767/`。

### 任务冒烟

复制 `task.json.example` 为 `task.json`，改 `imageFolder` / `outputExcel` / `exportPath` 后：

```powershell
Get-Content task.json -Raw -Encoding utf8 | uv run python run.py --plugin-id 90099 --port 18768
```

## 推荐使用顺序

1. 启动长驻服务，打开控制台
2. 「打开登录」完成 1688 扫码登录
3. 填写图片文件夹，`maxImages=1` 试跑
4. 确认结果 Excel 无误后批量跑（`maxImages=0`）；不要开启无头浏览器
5. 若页面要求验证，先在浏览器中手动处理，再点「人工处理后继续」。插件不会自动过验证、伪装浏览器或模拟人工轨迹

## 33万张图片任务

- `maxImages=0` 表示登记并处理文件夹中的全部图片。
- 断点文件保存在 `storage/jobs/image_job_*.sqlite3`，不要在任务完成前删除。
- 最终 Excel 只在断点库中所有图片都有结果后流式导出；中途关闭后再次使用同一文件夹和 TopN 会自动续跑。
- 结果 Excel 在最终导出期间不要用 Excel 打开；若目标文件被占用，程序会写入 `.plugin-result.xlsx` 旁路文件。
- 每个待处理文件都计为一次实际图搜请求；任务断点只跳过该文件自身已经成功完成的结果。
- 用户停止或发生可处理的中断时，会把断点库中当前已处理结果导出为阶段 Excel；后续续跑完成后再覆盖为完整结果。

## 打包上架

按平台文档打 ZIP：**根目录**直接可见 `manifest.json`（勿多套一层文件夹）。

```powershell
# 已生成发布包示例：
# dist/1688-image-sourcing-0.2.5.zip
```

发布包应包含：`manifest.json`、`run.py`、`index.html`、`pyproject.toml`、`uv.lock`、`src/`、`.env.example`、`README.md`、`task.json.example`、`storage/.gitkeep`。

**勿包含**：`.venv/`、真实 `.env`、`storage/browser_profile*`、`storage/debug/`、`storage/logs/*.log`、`__pycache__`、本地调试脚本。

安装后宿主会 `uv sync`；支持系统 Python 3.10 及以上版本。本机还需：

```powershell
uv run playwright install chromium
```

（若宿主注入 `PLAYWRIGHT_CHROMIUM_EXE` 则可跳过。）

## 私有仓库与数据安全

- 仓库只保存源码、测试、依赖锁文件和示例配置。
- `.env`、`task.json`、浏览器资料、Cookie、日志、断点数据库、结果表格、备份包及构建产物均由 `.gitignore` 排除。
- `storage/` 中的登录态和任务断点只保存在本机；换电脑后需要重新登录，未完成任务也不会随 Git 自动迁移。
- 即使使用 GitHub 私有仓库，也不要强制提交被忽略的浏览器资料或真实配置。

首次推送到新建的 GitHub **Private** 仓库时：

```powershell
git add .
git commit -m "Initial private import"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

## 说明

1688 页面结构可能变化，图搜上传控件若失效，需调整 `src/image_search.py`。首次务必有头模式登录。

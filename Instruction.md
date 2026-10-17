# 操作指南（本地运行 / 服务器部署）

本文件是**分步操作手册**。项目功能与配置字段说明见 [ReadMe.md](ReadMe.md)。

**你的服务器环境**：用户 `bit`，项目目录 `~/projects/Discipline_Inspection_Crawler`（即 `/home/bit/projects/Discipline_Inspection_Crawler`），当前 shell 处于 conda 的 `(base)` 环境。第 3 章的命令按这个环境写好，且**代码、虚拟环境、数据、日志、浏览器都限制在该项目目录内**。


## 目录

- [0. 快速开始](#0-快速开始)
- [1. 前置条件](#1-前置条件)
- [2. 本地运行（Windows）](#2-本地运行windows)
- [3. 服务器部署（Linux）](#3-服务器部署linux)
- [4. 运行后的验收清单](#4-运行后的验收清单)
- [5. 日常运维](#5-日常运维)
- [6. 常见问题排查](#6-常见问题排查)
- [7. 命令行参数与常见组合](#7-命令行参数与常见组合)

各参数含义

|参数|	含义|
|---|---|
|不带参数|	启动 UI + 定时并常驻；首次（无已登记文档）自动全量爬取并上传，之后等定时时间点|
|--config PATH	|指定配置文件（默认项目下的 config.yaml）|
|--output-dir PATH	|覆盖配置里的输出目录|
|--crawl-now	|启动时立即跑一次爬取（等价界面点开始），跑完继续常驻|
|--source all|ccdi|bit	|只配合 --crawl-now 指定来源，默认 all；不影响定时任务|
|--upload-now	|启动时立即上传全部未上传文件（忽略 3 天保留期），然后常驻|
|--no-ui	|不启动界面|
|--no-schedule	|不注册定时任务，只在同时加 --no-ui 时生效（开着界面就会启动定时，避免"设了时间却不执行"）|
|--no-auto-start	|首次不自动全量爬取，改由界面手动触发|


补充：
1. 参数可任意组合、顺序无关；
2. 「上传方式/爬取方式/定时设置」都在配置或界面里，没有对应命令行参数。

常见组合

|目的	|命令|
|---|---|
|按配置常驻 + UI 实时反馈 + 立刻跑一次	|python main.py --crawl-now|
|同上但只跑一个来源	|python main.py --crawl-now --source ccdi|
|常驻但先不跑，只等定时|	python main.py|
|先补传一次再常驻	|python main.py --upload-now|
|无界面常驻（只跑定时）	|python main.py --no-ui|
|纯单次、跑完退出	|python main.py --crawl-now --no-ui --no-schedule|
|换配置/输出目录	|python main.py --config ./config.yaml --output-dir /data/regulations|


服务器上就用第 3.6 节的 tmux/systemd 跑这条命令；systemd 里把 ExecStart 的末尾加上 --crawl-now，开机自启时会先立刻跑一次再转入定时模式（这一条我写进指南了）。

## 0. 快速开始

**服务器（Linux，你的 `bit@bit-Z790-D`）**

```bash
cd ~/projects/Discipline_Inspection_Crawler
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
.venv/bin/python -m playwright install chromium
.venv/bin/python main.py --no-auto-start --no-schedule
```

**本地（Windows，开发机）**

```powershell
cd E:\Program\Python\Program\Discipline_Inspection_Crawler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
python main.py --no-auto-start --no-schedule
```

## 1. 前置条件

| 项目 | 要求 |
|------|------|
| Python | 3.10 及以上（建议 3.11/3.12） |
| 磁盘 | 中纪检全量约数千份 PDF，预留 ≥ 5 GB |
| 网络 | 能访问 `www.ccdi.gov.cn`、`jwjc.bit.edu.cn`，以及你的 RAGFlow 地址 |
| RAGFlow | 已部署可用，拿到 `http://<host>:<port>/api/v1` 形式的地址与 API Key |
| 中文字体 | Linux 必须装 CJK 字体，否则导出的 PDF 里中文是方框 |

## 2. 本地运行（Windows）

### 2.1 安装依赖

```powershell
cd E:\Program\Python\Program\Discipline_Inspection_Crawler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

> 若 PowerShell 不允许执行脚本，先执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`；或干脆不激活，直接用 `.\.venv\Scripts\python.exe` 调用。

### 2.2 修改 config.yaml

至少确认这几项：

```yaml
output_dir: "E:/data/regulations"
ragflow:
  base_url: "http://10.1.1.126:9222/api/v1"
  api_key: "ragflow-xxxxxxxx"
  dataset_name_ccdi: "中纪检"
  dataset_name_bit: "校纪检"
schedule:
  crawl_time: "02:00"
  upload_time: "03:30"
upload:
  mode: "auto"        # auto=爬取结束后自动上传（默认）；manual=只在点「立即上传」时上传
crawl:
  # 每条之间的随机间隔（秒）：min~max 之间随机取值，避免固定节奏被识别
  request_delay_min_seconds: 1.0        # 中纪检（有反爬）
  request_delay_max_seconds: 3.0
  bit_request_delay_min_seconds: 0.5    # 校纪检（无反爬，可短一些）
  bit_request_delay_max_seconds: 1.0
  detail_page_delay_min_seconds: 1.0    # 中纪检每导出一条详情页后额外停顿（秒）
  detail_page_delay_max_seconds: 2.0
  rotate_user_agent: true               # 中纪检 UA 池随机轮换（每个分类/每条详情页）
  # 中纪检出现「您的请求过于频繁」限流页时逐轮的退避等待（单位：分钟）
  throttle_wait_ranges:
    - [10, 15]      # 第 1 次：等久一点，让站点限流窗口过去
    - [2, 3]        # 第 2 次
    - [1, 2]        # 第 3 次
  throttle_max_rounds: 3                # 连续退避 3 次仍限流则报错并暂停本轮（成功即归零）
  skipped_category_retry_minutes: 30   # 中纪检被跳过的子分类，多少分钟后自动只重爬这些分类（0=不自动重试）
  skipped_category_retry_max_rounds: 3 # 同一批跳过分类最多自动重试几轮（0=不限）
  # 按编号方式（crawl_mode: id_walk）：断点续跑 + 每天往后探测多少个编号
  crawl_mode: "id_walk"
  id_walk_max: 71069
  id_walk_daily_span: 400
  id_walk_invalid_streak_stop: 0       # 连续空号提前收手；0=不提前结束（推荐）
  # 仅 category 方式使用：中断后在该小时数内重启会从断点接着爬（0=每次全量扫描）
  resume_max_age_hours: 12
```

其它保持默认即可（`crawl.parallel_sources: true` 两站并行；`crawl.pdf_scale_bit` 控制校纪检 PDF 缩放）。

### 2.3 首次试跑：只开界面，手动测试

```powershell
python main.py --no-auto-start --no-schedule
```

- `--no-auto-start` 跳过首次全量爬取，改由界面手动触发。
- `--no-schedule` 只在**同时加了 `--no-ui`**（纯单次执行）时才真正跳过定时任务；**只要开着监控界面，定时任务就会启动**。所以用界面配置的「每日/间隔」时间在带界面的情况下一定会执行。
- 想隔离试跑数据：`python main.py --no-auto-start --no-schedule --output-dir .\data\test-run`

**在界面（<http://127.0.0.1:8765>）按顺序验证：**

1. 「学科智能体信息」卡片点「手动刷新」，确认能连上 RAGFlow、两个库的文件数与状态标签正常；连不上就改地址/Key 后点「保存配置」。
2. 切到「测试模式」：来源选「中纪检」或「全部」，条数填 `2`~`5`，上传方式选「手动」；点顶部 ▶ 开始，观察卡片进度与「历史记录」是否实时新增。
3. 到 `output_dir/中纪检/...` 检查 PDF：中文不是方框、内容没被右边界截断。
4. 确认无误后点「立即上传」，观察顶部进度条，再刷新「学科智能体信息」看文件数是否增加。
5. 需要时用顶部 ■ 终止、⏸ 暂停、▶ 继续。

> 测试模式每轮只取**还没爬过**的条目，反复点开始会不断往后新增，正好用来验证增量逻辑。

> 测试模式支持两种**定时方式**（同时管爬取与上传）：`按间隔`（每 N 分钟/小时一次）或 `按每日时间点`（直接在同一行里填写「每日爬取时间 / 每日上传时间」，留空=不启用）。选「按每日时间点」后，到点触发的爬取仍使用测试模式的来源与条数，上传则推送全部未上传文件；此时间隔任务停用。两种时间点与运行模式共用同一组配置。
>
> 「上传方式（手动/自动）」和「爬取方式（顺序/并行）」都在页面顶部的**总控制**卡片里，两个模式共用；下方「今日 / 总量」卡片会显示「首次执行 / 日常执行」标签，所有时间标签格式为 `YYYY-MM-DD HH:MM:SS`。

### 2.4 正式运行

```powershell
python main.py
```

启动监控界面 + 每日定时任务：

- 状态文件里「首次执行」标记还在（没有任何已登记文档时自动置位，中纪检编号扫到上限且校纪检跑完才清除）→ 判定为**首次运行**：页面顶部显示黄色「首次运行」标注，程序一边全量爬取两站、一边**每 15 秒推一批已落盘的新文件**（上传与爬取并行，不空等），爬取结束后再收尾一次。**中途中断重启后这个标记还在，所以续跑时边爬边传照常生效**，不会因为目录里已经有文件就退回定时上传；
- 已有数据 → 直接进入定时模式：每天 `schedule.crawl_time` 增量爬取、`schedule.upload_time` 上传（只上传已保存满 `upload.min_age_days` 天的文件）。

界面里可随时改「每日爬取时间 / 每日上传时间」，立即生效并写回 `config.yaml`。

### 2.5 常用命令行操作

```powershell
python main.py --crawl-now --source ccdi --no-ui --no-schedule
python main.py --crawl-now --source bit  --no-ui --no-schedule
python main.py --upload-now --no-ui --no-schedule
python main.py --output-dir E:\data\regulations
```

**终端跑、界面看**：界面每次刷新会热重载状态文件，可以两个终端配合：

```powershell
# 终端 A：只开界面
python main.py --no-auto-start --no-schedule
# 终端 B：执行任务，进度实时出现在终端 A 的页面上
python main.py --crawl-now --source ccdi --no-ui --no-schedule
```

## 3. 服务器部署（Linux）

以下命令**全部在项目目录内操作**：`/home/bit/projects/Discipline_Inspection_Crawler`。

### 3.1 确认 Python（注意 conda）

你的提示符是 `(base) bit@bit-Z790-D:~/projects/Discipline_Inspection_Crawler$`，说明当前在 conda base 环境里。这不冲突，但要注意两点：

- 项目内建好虚拟环境后，**一律用 `.venv/bin/python` 显式调用**，不要依赖 `python` 这个名字，避免落到 conda base 的 Python 上；
- 建虚拟环境用的 Python 需 **3.10+**。

```bash
cd ~/projects/Discipline_Inspection_Crawler
python3 --version
```

若是 3.10+（conda 的也行）直接使用。若低于 3.10（老系统常见 3.6/3.8），先装新版再建虚拟环境：

```bash
# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv
python3.12 -m venv .venv
# CentOS / RHEL / Rocky： sudo dnf install -y python3.11 python3.11-pip && python3.11 -m venv .venv
```

顺便把中文字体装上（**不装 PDF 中文会变方框**）：

```bash
sudo apt-get install -y fonts-noto-cjk          # Ubuntu / Debian
# CentOS/RHEL： sudo yum install -y google-noto-sans-cjk-fonts
# openEuler/Fedora： sudo dnf install -y wqy-zenhei-fonts
fc-list :lang=zh | head -3                       # 应能列出中文字体
```

字体装在系统目录里，不影响“操作限制在项目内”这一要求。

### 3.2 在项目内建虚拟环境并装依赖

```bash
cd ~/projects/Discipline_Inspection_Crawler

# 1) 虚拟环境建在项目里
python3 -m venv .venv

# 2) 用虚拟环境里的 python 装依赖（不要用 conda 的 python）
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

# 3) 关键：让 Playwright 的浏览器也下载到项目目录内（注意拷贝到服务器上的时候，要把在Windows下下载得到的.playwright-browsers文件夹删掉）
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
# 用镜像下，不然很慢
export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
.venv/bin/python -m playwright install chromium
```

> **关于 `PLAYWRIGHT_BROWSERS_PATH`（重要）**
> Playwright 默认把浏览器装到 `~/.cache/ms-playwright`（项目外）。为了“全部限制在项目内”，这里把它指向项目里的 `.playwright-browsers`。
> **安装与运行必须使用同一个值**，否则运行时它会去默认目录找浏览器并报 `Executable doesn't exist`。3.4、3.5 的每条启动命令都会带上这句。
> 如果你不介意浏览器放在 `~/.cache`，可以完全不设这个变量，其余步骤不变（本项目只在 Windows 下才会自动使用项目内浏览器目录）。

如果启动浏览器报缺系统库（`libnss3.so`、`libasound.so` 之类），补装一次（唯一需要 sudo 的步骤，不会往项目里写东西）：

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

### 3.3 自检：浏览器 + 中文渲染

```bash
cd ~/projects/Discipline_Inspection_Crawler
mkdir -p logs data
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
p = sync_playwright().start()
b = p.chromium.launch(headless=True)
pg = b.new_page()
pg.set_content("<h1>中文渲染自检</h1>")
pg.pdf(path="logs/font_check.pdf")
b.close(); p.stop()
print("browser ok -> logs/font_check.pdf")
PY
```

打开 `logs/font_check.pdf`：标题显示「中文渲染自检」而不是方框，说明字体与浏览器都就绪。
查看可以在本地机Windows的powershell或cmd中用scp命令传输：
```bash
C:\Users\Cecil>scp bit@10.1.1.126:~/projects/Discipline_Inspection_Crawler/logs/font_check.pdf D:\Code\AAA-AddSource\
```

### 3.4 修改 config.yaml

```yaml
output_dir: "/home/bit/projects/Discipline_Inspection_Crawler/data/regulations"

crawl:
  playwright_headless: true       # 服务器无桌面，保持无头
  browser_channel: ""             # 服务器一般没装 Chrome，留空用自带 Chromium
  parallel_sources: true
  stealth: true
  rotate_user_agent: true        # 中纪检 UA 池随机轮换（内置 Chrome/Edge/Firefox 的 Windows UA）
  pdf_scale_bit: 0.7              # 校纪检 PDF 缩放，越小越不容易超出右边界
  request_delay_min_seconds: 1.0  # 中纪检每条之间的随机间隔（秒）
  request_delay_max_seconds: 3.0
  bit_request_delay_min_seconds: 0.5  # 校纪检每条之间的随机间隔（秒）
  bit_request_delay_max_seconds: 1.0
  detail_page_delay_min_seconds: 1.0  # 中纪检每导出一条详情页后额外停顿（秒）
  detail_page_delay_max_seconds: 2.0
  throttle_wait_ranges:           # 中纪检限流退避的逐轮等待（分钟）
    - [10, 15]                    # 第 1 次 10~15 分钟
    - [2, 3]                      # 第 2 次 2~3 分钟
    - [1, 2]                      # 第 3 次 1~2 分钟
  throttle_max_rounds: 3          # 连续退避次数上限（不写时等于区间条数；成功即归零）
  skipped_category_retry_minutes: 30   # 中纪检被跳过的子分类，多少分钟后自动只重爬这些分类（0=不自动重试）
  skipped_category_retry_max_rounds: 3 # 同一批跳过分类最多自动重试几轮（0=不限）

schedule:
  crawl_time: "02:00"
  upload_time: "03:30"
  timezone: "Asia/Shanghai"       # 服务器时区不一致时务必显式指定

monitor:
  host: "0.0.0.0"                 # 需要远程访问就保持 0.0.0.0
  port: 8765

ragflow:
  base_url: "http://<RAGFlow地址>:<端口>/api/v1"
  api_key: ""                     # 建议留空，改用环境变量 RAGFLOW_API_KEY（见 3.5/3.6）
  dataset_name_ccdi: "中纪检"
  dataset_name_bit: "校纪检"
```

> API Key 二选一：写进 `config.yaml`（记得 `chmod 600 config.yaml`），或留空并用环境变量 `RAGFLOW_API_KEY` 注入。

### 3.5 首次试跑（只开界面，手动验证）

```bash
cd ~/projects/Discipline_Inspection_Crawler
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
export RAGFLOW_API_KEY="ragflow-xxxxxxxx"        # config.yaml 已填 Key 可省略
.venv/bin/python main.py --no-auto-start --no-schedule
```

浏览器打开 `http://<服务器IP>:8765`，按 2.3 的清单验证一遍（刷新智能体信息 → 测试模式抓 2 条 → 看 PDF → 立即上传）。

想完全隔离试跑数据：

```bash
.venv/bin/python main.py --no-auto-start --no-schedule --output-dir ./data/test-run
```

验证通过后 `Ctrl+C` 停掉，再用下面任一方式常驻运行。

### 3.6 常驻运行（三选一）

**方式一：tmux —— 推荐，无需 sudo，一切都在项目内**

```bash
cd ~/projects/Discipline_Inspection_Crawler
mkdir -p logs
tmux new -s crawler
# —— 进入 tmux 会话后执行 ——
cd ~/projects/Discipline_Inspection_Crawler
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
export RAGFLOW_API_KEY="ragflow-xxxxxxxx"
.venv/bin/python main.py 2>&1 | tee -a logs/app.log
# 按 Ctrl-b 松开再按 d 脱离；回来：tmux attach -t crawler
```

**方式二：nohup 后台 —— 无需 sudo，日志写在项目内**

```bash
cd ~/projects/Discipline_Inspection_Crawler
mkdir -p logs
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
export RAGFLOW_API_KEY="ragflow-xxxxxxxx"
nohup .venv/bin/python main.py >> logs/app.log 2>&1 &
echo $! > logs/app.pid
# 看日志：tail -f logs/app.log      停止：kill $(cat logs/app.pid)
```


**方式三：systemd —— 开机自启 + 崩溃自动重启；需要 sudo（会在项目外放一个 unit 文件，日志仍写在项目内）**

```bash
sudo tee /etc/systemd/system/discipline-crawler.service > /dev/null <<'EOF'
[Unit]
Description=Discipline Inspection Crawler (CCDI + BIT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bit
WorkingDirectory=/home/bit/projects/Discipline_Inspection_Crawler
Environment=PLAYWRIGHT_BROWSERS_PATH=/home/bit/projects/Discipline_Inspection_Crawler/.playwright-browsers
Environment=RAGFLOW_API_KEY=ragflow-xxxxxxxx
ExecStart=/home/bit/projects/Discipline_Inspection_Crawler/.venv/bin/python /home/bit/projects/Discipline_Inspection_Crawler/main.py
Restart=always
RestartSec=10
StandardOutput=append:/home/bit/projects/Discipline_Inspection_Crawler/logs/app.log
StandardError=append:/home/bit/projects/Discipline_Inspection_Crawler/logs/app.log

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /home/bit/projects/Discipline_Inspection_Crawler/logs
touch /home/bit/projects/Discipline_Inspection_Crawler/logs/app.log
sudo systemctl daemon-reload
sudo systemctl enable --now discipline-crawler
systemctl status discipline-crawler
# 改完 config.yaml 后：sudo systemctl restart discipline-crawler
```

> 注意：`Environment=` 的值不要加引号；若 systemd 版本较老不支持 `append:`，把两行 `StandardOutput/StandardError` 删掉，改用 `journalctl -u discipline-crawler -f` 看日志（此时日志在系统 journal 里，不在项目内）。

### 3.7 两个环境变量必须处处一致

| 变量 | 值 | 作用 |
|------|-----|------|
| `PLAYWRIGHT_BROWSERS_PATH` | `/home/bit/projects/Discipline_Inspection_Crawler/.playwright-browsers` | 浏览器装在项目内、运行时也能找到 |
| `RAGFLOW_API_KEY` | 你的 RAGFlow Key | 覆盖 `config.yaml` 里的 `api_key`，避免密钥落盘 |

手动运行、tmux、nohup、systemd 四处都要带上；一旦不一致，会出现「浏览器找不到」或「上传 401」这类问题。

### 3.8 防火墙 / 远程访问

```bash
sudo ufw allow 8765/tcp        # 或只放行内网网段：sudo ufw allow from 10.0.0.0/8 to any port 8765 proto tcp
```

监控界面用的是 Flask 自带开发服务器，内网自用足够；若要对外发布，建议前面加 Nginx 反向代理。

### 3.9 升级 / 重装

```bash
cd ~/projects/Discipline_Inspection_Crawler
# 更新代码（git pull 或覆盖文件）后：
.venv/bin/python -m pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
.venv/bin/python -m playwright install chromium
# 重启
sudo systemctl restart discipline-crawler     # systemd 方式；tmux 方式则 Ctrl-C 后重新执行启动命令
```

> **从旧版本升级后第一次务必强制全量**：旧版本存在"分类页只抓第 1 页"的缺陷（每个分类约 10 条，16 个分类合计约 128 条就结束了）。升级后请把 `crawl.resume_max_age_hours` 临时设为 `0`（= 每次都从头全量扫描），或者删掉 `crawler_state.json` 里的 `crawl_cursor` 段，跑完一轮再改回 `12`——否则状态里残留的续爬位置可能让程序从中间分类接着跑，跳过前面的分类。

## 4. 运行后的验收清单

- [ ] `http://<主机>:8765` 能打开，5 秒内出数据。
- [ ] 「学科智能体信息」卡片手动刷新成功，两个库文件数与状态标签正常。
- [ ] 测试模式抓 2 条：PDF 落盘、分类目录正确、**中文不是方框**、内容没被右边界截断。
- [ ] 「立即上传」后顶部进度条走完，知识库文件数增加，状态由「上传中」变「已完成」。
- [ ] 首次采集后 `output_dir/crawler_state.json` 里有 `documents` 记录。
- [ ] 再跑一次同样的爬取 → 新增应为 0（增量生效，不重复下载）。
- [ ] 运行模式下「下一次爬取 / 下一次上传」时间符合预期；把某个时间留空 → 对应定时任务消失。
- [ ] 服务器上进程稳定在跑（`systemctl status` / `tmux ls` / `tail -f logs/app.log`），日志没有反复报错。

## 5. 日常运维

| 需求 | 做法 |
|------|------|
| 改每日爬取/上传时间 | 界面「运行模式」里改，立即生效并写回配置；或改 `config.yaml` 后重启 |
| 只跑一次增量 | `python main.py --crawl-now --no-ui --no-schedule`（可加 `--source ccdi`/`bit`） |
| 立即补传所有待上传文件 | 界面点「立即上传」，或 `python main.py --upload-now --no-ui --no-schedule` |
| 换输出位置 | 界面「保存位置」+「应用保存位置」（需无任务运行），或改配置后重启 |
| 暂停 / 继续 / 终止 | 界面顶部 ⏸ / ▶ / ■ |
| 查某条是否已上传 | 「历史记录」列表看「已上传」标签，或查 `crawler_state.json` |
| 全部重新爬取并重传 | 删除 `output_dir/crawler_state.json` |
| 只重下某个文件 | 删除对应 PDF，下次爬取会补齐（不计新增） |
| 备份 | 备份 `output_dir`（PDF + `crawler_state.json`）即可 |

## 6. 常见问题排查

**提示「中纪检类目页触发人机验证」/ 顶部红色横幅**

程序已默认用反自动化参数规避。若仍被拦截：① 把 `crawl.playwright_headless` 改为 `false`，重启后手动完成一次滑动验证；② 把 `crawl.browser_channel` 设为 `chrome`/`msedge`（需已安装）；③ 确认 `crawl.stealth: true`。

**点了「开始」图标变成暂停，但中纪检进度不动**

1. 看卡片文案：如果写着「正在访问「全部数据」页估算总数（可能需要几分钟）…」，说明正在算 6444 这个分母（首次运行、且没有缓存时才会发生），等它算完即可；下次会直接用缓存，不再访问 `law_pagenumb`。
2. 如果文案是「按编号爬取：从 N 号开始」，说明已进入正常抓取，只是当前这个编号页面响应慢；卡片网址行会显示「请求中 + 目标地址」，单个页面最多等 45 秒，超时会用 `commit` 方式重试一次。
3. 如果文案是「[编号 N] 正在打开详情页…」但网址长时间不变，先在服务器上看日志：`tail -f logs/app.log`，日志里会有「编号 N 打开较慢：xx 秒」。
4. 如果卡片显示「运行中」而顶部终止按钮是灰的：旧版本在线程异常退出后会留下这种"假运行"。新版本会在异常时自动改回「空闲（爬取异常中止：…）」，重启时也会复位残留状态；仍遇到时重启进程即可。

**「学科智能体信息」连接失败**

检查 `base_url` 是否带 `/api/v1`、Key 是否正确；服务器上验证：

```bash
curl -s http://<host>:<port>/api/v1/datasets -H "Authorization: Bearer <key>" | head -c 300
```

网络不通时爬取仍会保存 PDF，恢复后点「立即上传」补传。

**报浏览器不存在（Executable doesn't exist）**

启动命令里漏了 `export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"`，或安装浏览器时与运行时用的值不一致。

**日志里出现「点击导航 law_validity_xxxx 失败，改用直接访问」或「分类 xxx 未取到列表，跳过」**

这是中纪检站点偶发的异常页（有时会跳到 `https://www.ccdi.gov.cn/error/error.html`）导致的，程序会自动处理：

1. 发现「没有左侧导航」「落到错误页」「没取到列表」时，**等待 2 秒 → 重新进入法规库首页 → 再跳转该分类**；
2. 同一分类最多重试 **3 次**，单次等待列表上限 **15 秒**（识别到 `/error/` 会立即重试，不用等满）；
3. 三次都失败才跳过该分类，并在日志里打印当时的 URL 与页面标题；期间若出现 `net::ERR_CONNECTION_RESET` 这类网络错误，只记日志、不会中断整轮爬取；
4. 被跳过的分类会登记到状态文件，并在界面**「中纪检」进度卡片**上列出分类名与原因，同时标注下一次自动重试时间；
5. 间隔 `crawl.skipped_category_retry_minutes` 分钟（默认 30）后，程序**只重爬这些被跳过的分类**（已爬过的自动跳过，不会重复下载），最多自动重试 `crawl.skipped_category_retry_max_rounds` 轮（默认 3）；之后等下一次完整爬取再试。点「终止」按钮会同时取消待执行的重试。

**如果 `/error/error.html` 上写着「您的请求过于频繁，请稍后再试」**（说明站点把当前 IP 限流了，中纪检大约每 20~30 条会出现一次），程序按下面的方式**原地退避**：

1. 按 `crawl.throttle_wait_ranges` **逐轮随机暂停**（默认第 1 次 10~15 分钟、第 2 次 2~3 分钟、第 3 次 1~2 分钟），然后立刻重试同一条/同一个分类；卡片显示「站点限流（请求过于频繁），暂停 X 分钟后重试（第 N/3 次，预计 2026-10-17 00:41:41）…」，括号里就是**下一次尝试的具体日期与时间**（日志里同步打印同样的时间）。第一次等久一点是为了让站点的限流窗口过去，之后通常只是瞬时抖动；
2. 等完仍被限流就**再等一次**（按下一轮的区间重新随机），连续 `crawl.throttle_max_rounds` 次（默认 3）后**报错并暂停本轮爬取**——日志打 ERROR、界面顶部弹红色横幅、未完成的分类登记到卡片上，交给定时重试在 `crawl.skipped_category_retry_minutes`（默认 30 分钟）后继续。最坏情况总等待约 15+3+2=20 分钟；
3. 中途只要成功一次（取到列表/导出成功），连续计数**立即归零**：所以下一次限流又是完整的 3 次机会；
4. 如果被限流时正好在导出详情页，程序会**删掉那张提示页 PDF 且不登记**，不会把垃圾内容推到知识库，也不会被误判成「已爬过」。

这类警告**不影响整体爬取**。如果 3 次退避仍常常用尽，说明站点对该 IP 限得比较紧：把中纪检的间隔调大（例如 `request_delay_min_seconds: 2` / `max: 5`）、把退避区间调大（例如 `throttle_wait_ranges: [[15,20],[5,8],[3,5]]`）、把 `crawl.skipped_category_retry_minutes` 调大（例如 60），或临时关掉 `crawl.parallel_sources` 减少并发后重跑一次（已爬的不会重复下载）。

**遇到限流页之后到底发生了什么？**

1. 检测到限流页（`/error/error.html`，正文写着"您的请求过于频繁"）→ 立刻记日志并在卡片上显示退避提示；
2. 按当轮区间随机等待（第 1 次 10~15 分钟 / 第 2 次 2~3 分钟 / 第 3 次 1~2 分钟），等待期间可以暂停（暂停不计入时长）或终止（立刻结束等待）；
3. 等到点后**马上重试**当前这条 / 这个分类：能拿到内容就继续正常爬，并把"连续退避次数"清零（下次再限流又是完整的 3 轮）；仍然限流就进入下一轮退避；
   - 等待途中被终止 / 程序被重启，**不会重新随机一个区间**：到点时间存在 `crawler_state.json` 里，重启后只补剩下的时间（卡片显示「继续上次未完成的限流等待：还剩 X 分钟（原定 …）」）；如果重启时已经过点，就从**下一轮**的短区间继续（第 2 轮 2~3 分钟），不会又从 10~15 分钟等起；
4. 3 轮都用完还是限流 → **报错并结束本轮**：日志打 ERROR、界面顶部弹红色横幅、当前及后续未爬的分类全部登记到「中纪检」卡片上；
5. 结束后的 30 分钟内程序会安排**只重爬这些分类**的任务（`crawl.skipped_category_retry_minutes`），同时续爬位置也留在被打断的分类上（12 小时内重启可接着爬）。

**日志出现「第 N 页共 0 条」「按翻页异常处理」怎么办？**

翻页是表单提交，页面返回后还会再跳一次，偶尔读到的只是过渡页（0 条）。程序会自动处理：**回到该类目页、重新翻到第 N 页再解析，最多 3 次**；成功就继续往下爬，不会中断。三次都取不到才把该分类按「翻页中断」跳过，并在本轮结束后安排 30 分钟后的重试（卡片上显示「爬取结束后安排重试」）。被跳过时该分类已爬过的条目不会重复下载，重试只补没爬到的部分。

**文件是怎么命名的？和网页上的条目怎么对应？**

- **中纪检**：`子分类[序号] 标题.pdf`，例如 `党章[1] 中国共产党章程.pdf`、`条例[12] 中国共产党党组工作条例.pdf`。序号是该条目在**这个子分类列表里的位置**（第 1 页第 1 条 = 1，跨页连续），每个子分类各自从 1 开始。
- **校纪检**：`[序号] 标题.pdf`，例如 `[17] 党政机关厉行节约反对浪费条例.pdf`。序号是整站 `法规制度` 列表的顺序（每页 8 条，所以 `index2.htm` 第 1 条是 17），可以逐条对照网页。
- 标题取的是列表页上显示的链接文字；Windows 不允许的字符会换成全角（`:`→`：`、`?`→`？`、`/`→`／`），英文双引号按开合换成中文引号（`"带病提拔"` → `“带病提拔”`），换行压成空格，其余保持原样。
- **已存在的文件不会改名**，编号只对之后新下载的文件生效；UI 的「历史记录」只显示标题本身，不带编号前缀。

**子分类面板里「部分完成」是什么意思？**

表示这个分类被标记为已完成，但已登记条数明显少于（精确的）总数，例如「10 / 53 部分完成」。说明那一轮没有真正爬全——常见原因是续爬只补了后半段、或某次翻页后页面没解析出条目（已加入保护：解析不到条目会按翻页异常处理并登记重试，不会再把该分类当成"已爬完"）。

处理办法：把 `crawl.resume_max_age_hours` 临时设为 `0`（强制从第 1 页全量扫描）跑一轮，已登记的文件会自动跳过、缺的会补齐；跑完后该分类会显示「53 / 53 完成」。另外分类的总数只有**从第 1 页完整扫完一遍**才会写成精确值，续爬只补后半段时不会把后半段的条数当成总数。

**某个子分类被跳过了，之后会自动重爬吗？**

会。被跳过的子分类会立刻写进状态文件，并显示在「中纪检」卡片的黄色清单里（分类名 + 原因 + 下次重试时间）。爬取结束后程序会安排一个**只重爬这些分类**的任务，默认 30 分钟后执行（`crawl.skipped_category_retry_minutes`）：已爬过的条目自动跳过，只补没爬到的部分；成功则该分类从清单里消失、任务取消；仍失败就按 `crawl.skipped_category_retry_max_rounds`（默认 3）再重试几轮，之后等下一轮完整爬取再补（每轮完整爬取都会把重试轮数清零）。

单看某一个分类被跳过、而下一个分类能正常爬，是正常的——这种跳过来自"未取到列表 / 页面打不开 / 翻页中断"（限流会走上面的退避流程，要么恢复要么结束整轮），跟分类本身无关，稍后重爬即可。注意：点「终止」按钮会取消待执行的重试；重启程序时若清单里还有分类，会自动重新安排。前提是程序用的是同一套 `output_dir`（状态文件在那里）。

**爬完了会自动上传吗？会不会等到明天？**

自动上传（`upload.mode: auto`）的规则是：

- **首次全量**：边爬边每 15 秒批量上传，**跑完再收尾上传一次**，立刻就能在知识库里看到；
- 之后设了每日上传点（比如 13:30）：爬取在 13:30 **之前**结束 → 等 13:30 的定时任务；爬取在 13:30 **之后**才结束（比如 13:31）→ **立即上传**，不会干等到第二天；
- 没设上传点 → 爬完立即上传；选「手动」→ 只在点「立即上传」时上传。

定时上传仍遵守 `upload.min_age_days`（默认 3 天）保留期；"立即上传"（含首次、含错过时间点）不受保留期限制。

**重启后界面还停在上一次的状态？**

先确认是不是**两个进程同时在跑**：

```bash
ss -ltnp | grep 8765          # 谁占着 UI 端口
ps -ef | grep "[m]ain.py"     # 有几个 main.py
```

最常见的两种情况：

1. **旧进程没退干净**（比如在 tmux / 另一个终端里挂着）：新进程的 UI 绑不上 8765，浏览器连的仍是旧进程——旧进程已死的话页面就一直停在旧数据上。`kill` 掉旧进程再启动即可。新版本在端口被占用时会打一条 ERROR 说明原因，不再静默失败。
2. **只有一个进程，但上次是被强杀的**：状态文件里留着 `running`。新版本启动时会自动把它改回「空闲（上次运行被中断）」，并把遗留的「爬取中」子分类标为「已中断」。旧版本可以手动点一次「开始」，或删掉 `crawler_state.json` 里的 `progress.ccdi.status`。

自检手段：启动日志会打一行 `进程启动：PID 12345 · 输出目录 … · 监控 UI http://0.0.0.0:8765`——日志里出现两个不同 PID 就是重复启动了。输出目录下的 `.crawler.pid` 记录当前实例；启动时若里面的 PID 还活着，会直接 WARNING 提示。

另外注意：`nohup .venv/bin/python main.py &` **不会自动继续爬**，它只是常驻并等定时任务（例如 12:00）。想立刻接着爬就在界面点「开始」（会先补上被跳过的分类），或启动时加 `--crawl-now`。

**任务结束变成「空闲」了，是跑完了还是断了？**

看卡片上那行结果文案，`空闲` 只是"当前没在跑"，不代表出错：

- 「完成，本次新增 128 个（分类 16/16 · 扫描 6300 条 · 用时 125 分钟）」→ **正常跑完**；
- 「已终止，停在 党内法规制度/办法，本次新增 …（…）」→ 被点了终止（或 Ctrl+C），停在哪个分类写得很清楚；
- 「站点持续限流，本轮提前结束 …（已登记 N 个分类待自动重试）」→ 限流 3 轮退避后仍不行，顶部同时会有红色横幅；
- 「被人机验证拦截 …」→ 需要切有头模式手动过验证。

中纪检的分类顺序是「党章 → 准则 → 条例 → … → 国际公约」，所以**最后一条落在「国家法律法规/国际公约」通常意味着整轮正常跑完**。日志里还会有一行汇总：「中纪检本轮结束（正常完成）：分类 16/16，扫描 6300 条，新增 128 个，用时 125.3 分钟」。

**卡片上的数字怎么看？**

中纪检卡片上有三处数字：

- 中间的大数字是 `累计已爬 / 库总量`。库总量来自「全部数据」页的「页码 × 每页条数」，会缓存 24 小时；缓存过期时优先沿用上次写进进度的分母，所以重启不会再为了算总数去访问 `/fgk/law_pagenumb`。大数字只在**新增文件**或**补写丢失的 PDF** 时前进。
- 下面一行是 `[编号 N · 已爬 M/6444] 标题`，N 是当前正在访问的 `law_display/<N>` 编号，M 是累计已爬数。
- 再下面是「本轮已扫描 M 条 · 打开详情页 K 个 · 新增 J 个」，以及当前网址 + 状态码。

**中断（Ctrl+C / 终止）后重启，会接着爬吗？**

会。编号位置保存在状态文件的 `id_walk.ccdi` 里：

```json
"id_walk": {
  "ccdi": {"last_id": 71088, "scanned_max": 71088, "failed_ids": [], "updated_at": "…"}
}
```

- 重启后先核对断点这一号（`last_url` 末尾的编号）是否**真的完成**：状态里有记录且 PDF 文件还在 → 从 `last_id + 1` 继续；没完成 → 只重试这一号，然后往下走。不会从 1 号重新扫；
- `scanned_max` 表示 1~N 号都已核对过（有效的已登记、无效的已确认没有），重启时这些编号直接跳过、**不发请求**；
- `failed_ids` 里是上次打开/导出失败的编号，重启时会补试；
- 每 20 个编号落一次盘，所以最多重复访问 20 个编号。

如果点「开始」后卡片立刻停在 `law_pagenumb` 不动，说明它正在估算库总量——新版本只有在**既没有缓存、也没有上次进度**时才会这样做（首次运行），并且卡片会写「正在访问「全部数据」页估算总数（可能需要几分钟）…」。

首次运行算出来的总量会写进状态文件的 `total_estimate.ccdi`，并且**在首次运行期间不过期**（全量扫完可能要几天），中断重启直接复用，不再去站点重算。

**文件名过长导致「异常中断」怎么办？**

Linux 单个文件名上限 255 字节，中文一个字 3 字节。旧版本遇到超长标题（例如「司法解释」里那种一长串联合发文）会在 `candidate.exists()` 抛 `OSError: [Errno 36] File name too long`，整轮爬取直接退出。现在：

- 文件名按 UTF-8 字节截断（整名 ≤ 200 字节），保留 `子分类[序号] ` 前缀，扩展名 `.pdf` 不变；
- 完整标题仍存在 `crawler_state.json` 里，历史记录里显示的是完整标题；
- 极少数仍写不进去的情况会记 ERROR 日志、把该编号记入 `failed_ids` 下次重试，不会再打断整轮。

卡片上出现红色「**异常中断**」标签时，原因就写在标签下面那行文案里（格式：`异常中断：异常类型: 消息`），同时顶部有红色横幅；重新点「开始」发起新一轮时横幅会收起，但卡片状态会保留到这一轮成功开跑。

**「首次执行」标签什么时候消失？**

`crawl_mode: id_walk` 时，必须同时满足：中纪检编号真的扫到 `crawl.id_walk_max`、校纪检完整跑完。只爬到一半就中断的话，重启后仍然显示「首次执行」（上传也继续走边爬边传），不会因为状态文件里有旧的 `pass_done: true` 就提前切换成「日常执行」。

> 以上适用于默认的「按详情页编号」方式（`crawl.crawl_mode: id_walk`）。如果用 `category` 方式（类目翻页），续爬位置是「当前分类 + 页码」，受 `crawl.resume_max_age_hours`（默认 12 小时）约束，并且会先把断点前被跳过的分类补一遍。

若发现序号不动而磁盘上不断出现新 PDF（或 `xxx_2.pdf` 这类副本），说明状态文件里的 PDF 路径与实际文件不一致：程序会**重写并修正记录**（算补写、计入进度、不算新增），若仍出现副本请把 `crawler_state.json` 与输出目录一起检查（两者的 `pdf_path` 必须指向同一目录，跨机器迁移时尤其要注意）。

**首次全量爬取途中撞上了设定的每日爬取时间，会怎样？**

**不会发生任何事**——定时任务是在首次全量爬取**结束之后**才注册的（`main()` 里先跑首次爬取，再启动调度器），所以那个时间点被直接跳过：不排队、不补跑，下一次是第二天的同一时间。这段时间的上传走的是首次运行专属的「边爬边每 15 秒批量上传 + 结束收尾」，与每日上传时间无关；界面上的「首次执行」标记会一直保留到中纪检编号扫到上限且校纪检完整跑完才切换成「日常执行」。

如果爬取是在调度器启动之后才开始的（例如 `--no-auto-start` 后手动点「开始」，或之后某天手动点「开始」），撞上定时点时日志会打「定时爬取跳过：已有任务运行中（这个时间点不再补跑，等下一天）」，同样不排队、不补跑。程序当时没在运行时，错过的时间点也不会补跑——想让某个时间点必定执行，就保证程序在那之前已经起来并常驻。

**PDF 中文变方框**

缺中文字体：`sudo apt-get install -y fonts-noto-cjk`（按发行版换包名），装完重启程序。

**PDF 内容超出右边界**

调小对应来源缩放：界面「测试模式」里的「校纪检 PDF 缩放」，或改 `crawl.pdf_scale_bit` / `crawl.pdf_scale_ccdi`。

**进度数字与预期不一致**

运行模式从「已爬取数量」累计、总数取可爬总数（中纪检按 `每页条数 ×（总页数−1） + 末页条数` 估算，校纪检取列表页「共 N 条」）；中纪检总数是估算值，日志会打印声明总数与实际统计数。

**界面打不开 / 端口占用**

改 `monitor.port`；查看占用：`ss -ltnp | grep 8765`。

**上传一直显示「上传中」**

RAGFlow 解析较慢会持续一段时间，点「手动刷新」看变化；长时间不动就去 RAGFlow 侧看文档解析状态。

**conda 干扰**

若 `python` 落到 conda，改用 `.venv/bin/python` 全路径调用即可；或先 `conda deactivate` 再操作。

## 7. 命令行参数与常见组合

### 7.1 每个参数的含义

| 参数 | 作用 | 备注 |
|------|------|------|
| （不带参数） | 启动**监控界面 + 定时任务**并常驻 | 首次运行（输出目录无已登记文档）会自动全量爬取并上传；已有数据则等定时时间点 |
| `--config PATH` | 指定配置文件 | 默认用项目目录下的 `config.yaml` |
| `--output-dir PATH` | 覆盖配置里的 `output_dir` | 建议服务器上用绝对路径 |
| `--crawl-now` | **启动时立即跑一次爬取** | 等价于在界面点「开始」：结束后按「上传方式」策略决定是否立即上传；跑完进程继续常驻 |
| `--source {all,ccdi,bit}` | 配合 `--crawl-now` 指定来源 | 默认 `all`；只影响 `--crawl-now`，不影响定时任务 |
| `--upload-now` | 启动时立即上传全部未上传文件 | 忽略 3 天保留期；之后进程继续常驻 |
| `--no-ui` | 不启动监控界面 | 与 `--no-schedule` 同时用即为"纯单次执行" |
| `--no-schedule` | 不注册定时任务 | **只在同时加 `--no-ui` 时生效**；开着界面时定时任务照常启动（避免界面里设了时间却不执行） |
| `--no-auto-start` | 首次运行时不自动全量爬取 | 改由界面手动触发；不影响定时任务 |

补充说明：

- 参数可以任意组合，顺序无关；`--source` 单独使用没有意义（它只作用于 `--crawl-now`）。
- 「上传方式」「爬取方式」「定时设置」都在 `config.yaml` 或界面里，**没有对应的命令行参数**——命令行只负责"启动方式"。其中上传方式对应 `upload.mode`（`auto`/`manual`，默认 `auto`），爬取方式对应 `crawl.parallel_sources`（`true`=并行）；界面上的开关改动都会写回 `config.yaml`。
- `--crawl-now` 的上传行为完全跟随「上传方式」：手动=只爬不传；自动且已设上传时间点=只爬、等定时任务上传；自动且未设时间点=爬完立即上传；首次采集=边爬边批量上传。

### 7.2 常见组合

| 你的目的 | 命令 |
|----------|------|
| **按配置文件运行 + 常驻 + UI 实时反馈，并立刻开始跑一次（等同界面点「开始」）** | `python main.py --crawl-now` |
| 同上，但只跑中纪检 / 只跑校纪检 | `python main.py --crawl-now --source ccdi`（或 `bit`） |
| 按配置文件运行 + 常驻 + UI，但**不**立刻跑，只等定时时间点 | `python main.py` |
| 首次部署，先起来但不自动全量（改在界面里点开始） | `python main.py --no-auto-start` |
| 先补传一次再常驻 | `python main.py --upload-now` |
| 换输出目录 + 指定配置文件 | `python main.py --config ./config.yaml --output-dir /data/regulations` |
| 无界面常驻（只跑定时任务） | `python main.py --no-ui` |
| 纯单次：无界面无定时，爬完即退出 | `python main.py --crawl-now --no-ui --no-schedule` |
| 纯单次上传 | `python main.py --upload-now --no-ui --no-schedule` |

### 7.3 你要的那条命令

> 需求：按 `config.yaml` 的参数运行、常驻执行、UI 同时正常动态反馈，相当于把"在界面点开始"这一步改到终端里做。

```bash
python main.py --crawl-now
```

它的完整行为是：

1. 启动监控界面（默认 `http://<主机>:8765`），界面从第一秒起就实时显示进度与历史记录；
2. 按 `config.yaml` 立即跑一次爬取（默认来源 `all`，即中纪检 + 校纪检，是否并行取 `crawl.parallel_sources`）；
3. 爬取结束后按「上传方式」策略处理上传（首次采集会边爬边批量上传）；
4. 之后进程**继续常驻**：定时任务照常运行（每日时间点 / 测试模式的间隔），界面继续刷新。

服务器上建议用第 3.6 节的 tmux 或 systemd 方式跑这条命令，例如 systemd 里把 `ExecStart` 改成：

```
ExecStart=/home/bit/projects/Discipline_Inspection_Crawler/.venv/bin/python /home/bit/projects/Discipline_Inspection_Crawler/main.py --crawl-now
```

这样开机自启时会先立刻跑一次，然后转入定时模式常驻。

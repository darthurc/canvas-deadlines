# Canvas Deadlines · 截止日期桌面挂件

把 Canvas LMS 的课程作业、截止时间和倒计时显示在桌面上。本次发布 Windows 版。

主要功能：最近任务倒计时、夏令时换算、手动标记已交、截止提醒、完整列表网页、拖动位置和可选开机自启。刷新失败会保留上次数据并显示警告。

## Windows 使用

如果下载的是 Windows 发布包：解压到自己有写入权限的普通文件夹，双击 `CanvasDeadlines.exe`，在设置窗口中粘贴**自己的**日历订阅链接。首次使用无须安装 Python。程序会在同目录生成个人配置和数据，请保留整个文件夹。

如果下载的是源码：

1. 安装 [Python 3.10 或以上](https://www.python.org/downloads/)，安装时选择 Tk 和 Python Launcher。
2. 双击 `install-windows.bat` 安装依赖。
3. 双击 `run-widget.bat`，在设置窗口中填写订阅链接。

获取订阅链接：Canvas → Calendar → 右侧 Calendar Feed → 复制链接。链接相当于读取课程日历的凭证，只在本机设置窗口粘贴，不要贴进 GitHub Issue、聊天、截图或公开文档。

## macOS

Mac 版暂不发布，待实机验证后再提供。

## 日常操作

- 拖动挂件标题栏改变位置；右键打开菜单。
- 点击刷新按钮或使用 `run-fetch.bat` 刷新。
- 右键某个作业，手动标记或取消“已交”。
- 开机自启可在设置中选择，也可以运行 `安装开机自启.bat` / `卸载开机自启.bat`。
- 文件夹移动后，请重新安装开机自启，让系统任务指向新位置。

日历订阅不包含真实提交状态。提醒会提示“请核对是否已交”，手动标记仅影响本机展示，不会向 Canvas 提交作业或更改学校数据。

## 配置和支持范围

默认显示时区为 `Australia/Melbourne`，按墨尔本课程日历设计。浮动时间按此时区解释，事件携带的 TZID 优先用于解析。其他学校用户需先核对时区和任务识别结果；本程序没有完整实现通用日历的重复规则（RRULE）、自定义 VTIMEZONE 或所有特殊事件格式。

`config.example.json` 是空凭证模板。运行时配置在 `config.json`，不要把实际配置改名成示例文件提交。

可选 API 模式：学校允许时，在本机 `config.json` 中填写 `canvas.base_url` 和 `canvas.token`。API token 优先于订阅链接，能读取官方提交状态；设置向导保存新的日历订阅时会清空旧 API token。

断网、失效凭证或刷新崩溃时，挂件保留旧任务并标红。仍应以 Canvas 官方页面为准，尤其是个人延期、补交、课程变动和学校日历更新延迟。

## 开发、测试和构建

```text
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python app.py --help
```

Windows 运行 `打包.bat` 生成 `dist/CanvasDeadlines.exe`。打包必须包含 `tzdata`，否则缺少系统时区数据的机器可能显示错误时间。

Mac 源码包：`python 打包Mac版.py`。它只包含明确列出的程序文件和空配置模板，排除所有运行配置、缓存和日志。

GitHub Actions 的 `.github/workflows/test.yml` 会在 Windows、macOS 和 Linux 上运行回归测试。这是持续验证配置，不代表所有平台已在本机验证。

## 隐私与发布

本项目的 `.gitignore` 沿用原项目规则，并补充配置备份、虚拟环境和临时文件的排除。以下内容不应提交：`config.json`、配置备份、`state/`、`out/`、`发布/`、`分享版/`、`dist/`、旧压缩包及可执行程序。可执行程序应放在 GitHub Releases 中。

`.gitignore` 不会移除已经被 Git 跟踪的文件。首次发布前务必核对 `git ls-files`。如果订阅链接已公开，在 Canvas Calendar Feed 中 Reset 并更新本机配置；删除文件或提交不足以撤销凭证。

日志及诊断报告可能包含课程名和作业标题。虽然程序会隐藏常见凭证格式，公开反馈问题前仍需检查并删去个人信息。

本次检查的具体修复见 [CHANGELOG.md](CHANGELOG.md)。

# Python RPA 报表采集器

第一阶段只做 RPA 原始采集，不解析或清洗数据。程序使用 Playwright 打开真实浏览器；首次运行时由用户手工登录并保存登录状态，随后按 YAML 配置访问报表页面、设置筛选条件并下载原始报表。

## 安装

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item config.example.yaml config.yaml
```

示例配置已使用淘宝星河入口 `https://adstar.alimama.com/`。复制为 `config.yaml` 后，根据登录后的实际页面补充报表地址和页面选择器。

## 运行

```powershell
python main.py --start-date 2026-09-01 --end-date 2026-09-03
```

浏览器弹出后，首次需要手工完成密码、短信、扫码或验证码登录；进入工作台后回到命令窗口按 Enter。`runtime/auth_state.json` 保存登录凭证，不要发送给其他人，也不要提交到版本库。登录失效后可删除该文件并重新运行。

输出文件位于 `output`。每次运行的任务状态清单位于 `logs/manifest_*.json`；失败任务的页面截图位于 `logs/screenshots`，运行日志保存在 `collector.log`。

## 配置动作

- `fill`：填写输入框，支持 `{start_date}`、`{end_date}`、`{run_date}` 和 `{run_time}`。
- `click`：点击页面元素。
- `click_role`：按元素角色和名称精确点击，例如链接“我的星河”；比坐标定位稳定。
- `locate_texts`：逐一精确定位一组页面文本，并把匹配数量写入日志。
- `visit_details`：按项目名称定位卡片，悬停后点击右侧详情图标，再返回列表。
- `select`：选择原生下拉框选项。
- `check` / `uncheck`：勾选或取消复选框。
- `press`：向元素发送按键，例如 `Enter`。
- `wait`：等待元素出现。
- `wait_ms`：必要时固定等待若干毫秒。

第一阶段的结果类型仅为 `download`：点击网站导出按钮并保存原始文件。网页表格解析、清洗和写入 Excel 模板留到第二阶段。

## 打包 Windows 程序

先确认源码模式工作正常，再安装 PyInstaller：

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --onedir --name ReportCollector main.py
```

Playwright 的浏览器文件体积较大，实际交付时建议采用 `--onedir`，并把浏览器安装和配置文件一起做成安装包；相比单文件模式更容易排查浏览器资源路径问题。

## 注意

- 页面选择器必须根据目标网站实际 DOM 调整，尽量选择稳定的名称、标签或测试 ID。
- 不绕过验证码或访问控制；采集范围和频率应符合账号权限及平台规则。
- 当前阶段不会修改下载到的 Excel、CSV 或其他报表文件。

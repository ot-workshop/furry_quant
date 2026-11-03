# 本地启动与卡死修复

2026-10-31。

```powershell
.\start.ps1
```

默认只启动网页与任务服务，选择活动配置的数据/结果目录；回测、因子计算和评估在网页“新建研究”提交。看到 `Uvicorn running on http://127.0.0.1:8000` 后打开该地址。终端持续运行以提供服务，保持打开，按Ctrl+C结束。

可选命令：

```powershell
.\start.ps1 -Port 8001
.\start.ps1 -Config configs\research.joint.json
.\start.ps1 -RunResearch -Config configs\research.joint.json
.\start.ps1 -Demo
```

`-Config`默认只选择目录，不计算。`-RunResearch`先运行配置中的回测和评估，再启动网页；`-Demo`先生成合成样本。两者均明确输出计算阶段，不自动下载数据。端口被占用时立即显示错误，不终止其他服务。

## 原因与修复

旧脚本每次启动都先执行real和evaluate，源码变更导致新指纹时需要完整计算，网页一直等到计算完成后才启动。脚本现在将计算与打开网页分开，默认不隐式生成或重算结果。

本机还复现了真正的依赖加载阻塞：Qlib导入SciPy，SciPy触发NumPy.testing，后者调用Python 3.12的platform.machine，进入platform._wmi_query后15秒仍无返回。调用栈保存于 `.agent-work/startup-diagnosis.log`。

新增 `finance_quant.runtime.configure_runtime`，在CLI及后台任务进程加载科学计算依赖前，针对Windows Python 3.12让platform的WMI查询抛出OSError，使用标准库已有的Windows API/环境信息回退。只影响当前进程的系统描述信息，不改Windows服务、Python安装或第三方库，也不改变行情、因子公式和撮合规则。Python 3.11、其他系统不应用该处理。数值线程默认1，已有设置保留；后台计算仍采用原有的单线程子进程设置。

脚本保留UTF-8 BOM以兼容PowerShell 5.1，并在运行期间使用UTF-8控制台输出，结束后恢复原控制台编码。无需配置此前仅供验收使用的QA环境变量。

## 验证

- 启动、配置、任务队列和十因子相关65项回归测试通过，包含7项新增启动测试。
- 在Windows PowerShell 5.1实际启动，约1.08秒健康接口就绪；PowerShell 7约1.06秒。两者首页、健康和十项因子选项均正常。隔离结果目录在启动时没有生成评分、回测或评估。
- 在实际服务提交五股真实快照任务，完成因子、原生Qlib回测、评估及对应结果查询。无QA回退开关，未调用付费接口。
- 显式 `-RunResearch` 使用单因子、2026年9月短区间配置，约5.84秒计算完成并启动服务，三类产物齐全。该耗时是短区间验收值，较长研究仍需相应计算时间。
- 占用端口时立即拒绝并提示换端口。所有隔离测试服务已关闭，用户活动配置与原结果保留。

真实启动记录为 `.agent-work/startup-live/evidence.json`，详细日志位于其work_dir所指目录。旧验收脚本清理进程时，taskkill /T也曾超时；该次只关闭本次明确创建的进程，后续测试改用原生进程API并通过。产品启动脚本不执行taskkill或终止其他进程。

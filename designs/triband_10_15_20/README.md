# 三波段 10 / 15 / 20 m 八木（单 boom，直接馈电）

按需求定制：**10 m 三单元（反射器 + 驱动 + 引向器）、15 m 与 20 m 各两单元（反射器 + 驱动）**，
全部单元装在**同一根 7.8 m boom** 上，**直接 50 Ω 馈电**（每个波段的驱动单元中心点由
`EX 0 <tag> <中心段> 0 1 0 0` 直接激励，不加任何匹配网络）。

* 坐标系：振子沿 **Y** 轴（半长对称），boom 沿 **+Z** 轴（主瓣指向 +Z）。
* 材料：铝管，半径 `0.010 m`（φ20 mm），`LD 5 0 0 0 3.5400E+07`（σ = 3.54×10⁷ S/m，即纯铝）。
* 环境：自由空间 `GN -1`，`EK`（扩展薄线内核），无地面、无馈线、无匹配损耗。
* 频率：`FR 0 0 0 0 <f> 0` 单频；球面方向图卡的网格为 `RP 0 37 73 1000 0 0 5 5`。

## 1. 结构布局（沿 boom，z 从 0 起）

```
 z/m   0.00      1.10        2.60   3.30          4.10          5.60          7.80
       20R       15R         10R    20D           15D           10D           10Dir
       |---------|-----------|------|-------------|-------------|-------------|--> +Z（主瓣）
       反射器     反射器      反射器  驱动(20m)     驱动(15m)     驱动(10m)      引向器
```

每个波段只看自己那 2（或 3）个单元：驱动单元在最前，反射器在其后面，其余波段的单元对它是
"更长的寄生元件"（10 m 波段的 20R/15R/20D/15D 长度都接近 28 MHz 的半波，会带来一定散射）。

## 2. 几何尺寸（可直接下料，单位 m）

| 单元 | 波段 | 角色 | 全长 L | 单臂长 L/2 | boom 位置 z | 建模段数 | NEC tag |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20R | 20 m | 反射器 | 10.6171 | 5.30855 | 0.000 | 41 | 1 |
| 15R | 15 m | 反射器 | 7.4490 | 3.72450 | 1.100 | 31 | 2 |
| 10R | 10 m | 反射器 | 5.2630 | 2.63150 | 2.600 | 21 | 3 |
| 20D | 20 m | 驱动 | 9.8789 | 4.93945 | 3.300 | 41 | 4 |
| 15D | 15 m | 驱动 | 6.6603 | 3.33015 | 4.100 | 31 | 5 |
| 10D | 10 m | 驱动 | 4.8630 | 2.43150 | 5.600 | 21 | 6 |
| 10Dir | 10 m | 引向器 | 4.3157 | 2.15785 | 7.800 | 19 | 7 |

boom 全长 **7.80 m**。波长归一化（按各自波段中心频率）：10R 0.500λ / 10D 0.462λ / 10Dir 0.410λ（28.5 MHz），
15R 0.527λ / 15D 0.471λ（21.2 MHz），20R 0.501λ / 20D 0.466λ（14.15 MHz）；
10 m 的三个单元间距 3.00 m / 2.20 m（0.285λ / 0.209λ @28.5 MHz）。

> 10 m 用 0.410λ 的短引向器（而不是教科书上的 0.42–0.44λ）是为了在 **28.0–29.7 MHz 全段把 SWR 压到 2 以下**，
> 代价是峰值增益 7.0 dBi 而不是 8.2 dBi。

## 3. 馈电与切换

* 三份 deck（`_10m.nec` / `_15m.nec` / `_20m.nec`）**各自只激励本波段的驱动单元**，另外两个驱动单元的
  `EX` 卡不存在（结构仍在、电气上开路）——这就是"直接馈电"的单波段工作状态。
* `EX` 位置：驱动单元中心段（10D tag6 段 11、15D tag5 段 16、20D tag4 段 21），段数与 boom 位置一一对应。
* 现场实现：三根馈线各接一个驱动单元中心（伽玛匹配/发夹都不用，实测 50 Ω 下带内 SWR ≤ 1.8），
  收发信机侧用一个三选一天线开关；不用的两个波段驱动单元悬空（继电器断开即可）。
* 平均效率 99.5 %–99.9 %（导体内阻损耗），可忽略；实际损失主要来自地面、陷波器、接头与雨雪。

## 4. 仿真结果（NEC-2，`nec2dxs11k.exe`，自由空间）

| 波段 | 中心频率 | Z (Ω) | SWR | 增益 (dBi) | 前后比 (dB) | 效率 | 带内 SWR（低端 → 高端） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10 m | 28.51 MHz | 47.3 − 10.8j | **1.256** | 7.00 | 17.6 | 99.82 % | 28.0: 1.716 → 29.7: 1.785（全段 < 2） |
| 15 m | 21.18 MHz | 42.0 − 0.5j | **1.192** | 5.74 | 11.9 | 99.75 % | 21.0: 1.330 → 21.45: 1.286（全段 < 1.6） |
| 20 m | 14.14 MHz | 51.6 − 1.4j | **1.042** | 6.29 | 11.3 | 99.69 % | 14.0: 1.309 → 14.35: 1.347（全段 < 2，13.9–14.55 < 2） |

带内增益范围：10 m 6.96–7.44 dBi、15 m 5.67–5.80 dBi、20 m 5.93–6.54 dBi。
主瓣都在 θ = 0°（沿 boom +Z 正前方）；后瓣在 θ = 180°。

## 5. 曲线与方向图

* 驻波 / 增益 / 前后比 / 阻抗 / Smith / 效率六合一（每张图都带「频率 (MHz)」坐标轴）：
  [`triband_10m_sweep.png`](../../docs/images/triband/triband_10m_sweep.png)（27.8–30.1 MHz）
  [`triband_15m_sweep.png`](../../docs/images/triband/triband_15m_sweep.png)（20.8–21.7 MHz）
  [`triband_20m_sweep.png`](../../docs/images/triband/triband_20m_sweep.png)（13.8–14.7 MHz）
* 仰角面（E 面，φ = 90°，0° = 沿 +Z 即 boom 正前方、90° = 垂直 boom）极坐标方向图：
  [`triband_10m_polar.png`](../../docs/images/triband/triband_10m_polar.png)
  [`triband_15m_polar.png`](../../docs/images/triband/triband_15m_polar.png)
  [`triband_20m_polar.png`](../../docs/images/triband/triband_20m_polar.png)
* 3-D 立体方向图（球面 37×73，5° 步进）：
  [`triband_10m_3d.png`](../../docs/images/triband/triband_10m_3d.png)
  [`triband_15m_3d.png`](../../docs/images/triband/triband_15m_3d.png)
  [`triband_20m_3d.png`](../../docs/images/triband/triband_20m_3d.png)

![10 m 扫频曲线](../../docs/images/triband/triband_10m_sweep.png)

> 仓库里的图放在 `docs/images/`；自己重跑 `nec_plot_*` 时默认写到 `plots/`（已 `.gitignore`）。

> 注意：对 boom 沿 +Z 的八木，`nec_plot_pattern` 的 `plane="azimuth"`（固定 θ = 90°）得到的是
> **垂直 boom 的平面**，增益只有 −0.8 dBi，不适合作为"方位方向图"；要看主瓣请用 `plane="elevation"` 或 3-D。

## 6. 架设高度与地面（真实地面 5 / 8 / 10 m）

把天线转到水平姿态（boom 沿 +X、振子沿 Y、全部导线在 `z = H`），地面为 NEC 的 `z = 0` 平面，
地面参数取平均值 `GN 2 0 0 0 13.0 0.005`（εr = 13、σ = 0.005 S/m），在 φ = 0（含 boom 的垂直面）
取 1° 步进的仰角剖面，与自由空间（`GN -1`）对比：

| 波段 | 条件 | 峰值增益 | 起飞角 | 10° 仰角增益 | 20° 仰角增益 |
| --- | --- | --- | --- | --- | --- |
| 10 m | 自由空间 | 7.00 dBi | 0° | 6.94 | 6.76 |
| 10 m | 真实地面 5 m | 11.34 dBi | 27° | 6.56 | 10.66 |
| 10 m | 真实地面 8 m | 12.07 dBi | 18° | 9.96 | 11.96 |
| 10 m | 真实地面 10 m | **12.33 dBi** | **15°** | 11.33 | 10.98 |
| 15 m | 自由空间 | 5.73 dBi | 0° | 5.70 | 5.60 |
| 15 m | 真实地面 5 m | 9.54 dBi | 36° | 2.94 | 7.65 |
| 15 m | 真实地面 8 m | 10.73 dBi | 24° | 6.79 | 10.46 |
| 15 m | 真实地面 10 m | **10.81 dBi** | **19°** | 8.24 | 10.81 |
| 20 m | 自由空间 | 6.27 dBi | 0° | 6.22 | 6.06 |
| 20 m | 真实地面 5 m | 8.61 dBi | 44° | 0.53 | 5.55 |
| 20 m | 真实地面 8 m | 10.04 dBi | 33° | 3.97 | 8.54 |
| 20 m | 真实地面 10 m | **10.77 dBi** | **28°** | 5.90 | 10.04 |

图（左：极坐标仰角面；右：0–60° 直角剖面，四条曲线 = 自由空间 / 5 m / 8 m / 10 m）：

* [`triband_10m_elevation_vs_height.png`](../../docs/images/triband/triband_10m_elevation_vs_height.png)
* [`triband_15m_elevation_vs_height.png`](../../docs/images/triband/triband_15m_elevation_vs_height.png)
* [`triband_20m_elevation_vs_height.png`](../../docs/images/triband/triband_20m_elevation_vs_height.png)

![10 m 波段：架设高度对仰角方向图的影响](../../docs/images/triband/triband_10m_elevation_vs_height.png)

结论：

* 架得越高，主瓣越低、峰值越高——10 m 高时 10 m 波段起飞角 15°、峰值 12.33 dBi（比自由空间高 5.3 dB，
  这是地面镜像的同相叠加增益，不是天线本身变强）。
* 追求 **低仰角 DX**：10 m 波段建议 ≥ 8 m（10° 仰角 9.96 dBi）；15/20 m 波段 5 m 高时 10° 仰角只有
  0.5–2.9 dBi，明显被地面抵消，需要 8–10 m 才顺。
* 追求 **高仰角近距离（NVIS / 中继）**：5 m 高时 20 m 波段在 44° 有 8.61 dBi，正好覆盖几百公里内。
* 地面损耗已包含在 NEC 的辐射功率里（GN 2 是反射系数法，功率表仍显示 100% "效率"），
  所以表中效率列在自由空间/地面两种情况下都接近 100%，不要把它当成地面损耗。

复现：

```powershell
.\.venv\Scripts\python.exe scripts\ground_study.py            # 三个波段全部
.\.venv\Scripts\python.exe scripts\ground_study.py --band 10m  # 只算一个
```

## 7. 文件

| 文件 | 内容 |
| --- | --- |
| `triband_10_15_20.nec` | master deck：10 m 激励 + 球面 RP 卡（可直接出 3-D） |
| `triband_10_15_20_10m.nec` / `_15m.nec` / `_20m.nec` | 各波段单独激励的 deck（扫频、方向图用这三份） |
| `design.json` | 结构化几何 + 带内 11 点扫描数据（SWR/增益/前后比/效率） |
| `design_summary.txt` | 同一份结果的可读文本 + 设计元信息 |

## 7. 复现与二次优化

```powershell
# 重新评估（13 点带内扫描 + 中心点指标）
.\.venv\Scripts\python.exe scripts\design_triband.py --report --out designs\triband_10_15_20

# 只针对某个波段再优化驱动/反射器长度（坐标搜索，12 档 × 6% × 3 轮）
.\.venv\Scripts\python.exe scripts\design_triband.py --optimize 10m --out designs\triband_10_15_20

# 出图（DSH 里也可以直接让 agent 调 MCP 工具）
.\.venv\Scripts\python.exe scripts\check-tools.py --call nec_plot_sweep `
  --arg path=designs/triband_10_15_20/triband_10_15_20_10m.nec `
  --arg start_mhz=27.8 --arg stop_mhz=30.1 --arg step_mhz=0.1 `
  --arg out_dir=plots/triband/10m
```

几何改动只需要改 `scripts\design_triband.py` 里 `initial_design()` 的元素列表（长度 z 位置段数），
再 `--report` 复算即可；`Design.deck()` 负责生成 NEC 卡（`GW`/`GE`/`EK`/`GN`/`LD`/`EX`/`FR`/`RP`/`EN`）。

## 8. 已知限制

* 自由空间模型（第 4 节）：地面（`GN 2` 真实地）与架设高度会改变仰角波瓣与增益，见第 6 节；
  实际架设建议 ≥ 1λ 高。
* 未建模：boom 与单元间的 U 形螺栓、绝缘子、馈线、绕组与继电器电容；这些会轻微降低前后比。
* 7.8 m boom 与 10.6 m 单元臂都比较长，需要中心支撑（boom 中部加撑杆），单元端部需防风摆。
* 三个波段共用一根 boom 时，未工作波段的寄生单元会让 10 m 增益比"单波段专用"低约 0.6 dB，
  这是嵌套设计的固有代价（本方案已通过缩短引向器补偿带内匹配）。

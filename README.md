# FFmpeg 性能基准测试工具

测试服务器在**不同内存 Channel 配置**下的 FFmpeg 编码性能，
生成可通过浏览器远端查看的 HTML 对比报告。

---

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.6+ | 标准库，无需额外安装 |
| FFmpeg | 需包含 libx264 / libx265 / libvpx-vp9 编码器 |
| dmidecode | 读取 DIMM 内存插槽信息（需 root 权限） |
| mbw（可选） | 内存带宽测试 `sudo apt install mbw` |

---

## 快速开始

```bash
# 1. 上传脚本到服务器
scp ffmpeg_benchmark.py user@<服务器IP>:/home/user/

# 2. SSH 登录服务器
ssh user@<服务器IP>

# 3. 安装可选依赖（内存带宽测试）
sudo apt install mbw

# 4. 运行测试（建议 sudo，用于读取内存 channel 信息）
sudo python3 ffmpeg_benchmark.py --label "4-channel"

# 5. 在笔记本新开终端，建立 SSH 隧道
ssh -N -L 8080:<服务器IP>:8080 user@<服务器IP>

# 6. 笔记本浏览器打开
# http://localhost:8080/report.html
```

---

## 内存 Channel 对比测试流程

```bash
# 第一次：服务器处于 4-channel 配置（每次约 30~40 分钟）
sudo python3 ffmpeg_benchmark.py --label "4-channel"

# 修改 BIOS/拔掉内存后，重启服务器，再次运行：
sudo python3 ffmpeg_benchmark.py --label "2-channel"

# 继续减少 channel：
sudo python3 ffmpeg_benchmark.py --label "1-channel"

# 每次运行完刷新浏览器，即可看到多配置对比图表
```

---

## 测试设计说明

| 测试类型 | 说明 |
|---------|------|
| 单实例 | 依次运行 11 个测试项，每项 60s，观察各编码器性能 |
| 高线程数 | 4K 编码时指定 -threads 32/64，最大化 CPU 和内存带宽压力 |
| 并行多实例 | 同时启动多个 FFmpeg 进程，合并统计总 FPS，真正压测内存带宽瓶颈 |
| mbw 内存带宽 | 使用 mbw 工具直接测量 MEMCPY/DUMB/MCblock 带宽，作为辅助参考 |

**视频源**：`testsrc2 + noise` 滤镜，加入随机噪声确保每帧不可预测，
避免编码器利用合成图案优化，使测试更贴近真实视频编码负载。

---

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--label TEXT` | 无（必填） | 本次测试的配置标签，如 `4-channel` |
| `--duration N` | `60` | 每项测试持续秒数（60s 确保 CPU/内存进入稳态） |
| `--output-dir PATH` | 脚本目录/benchmark_results | 结果和报告的保存目录 |
| `--port N` | `8080` | HTTP 报告服务端口 |
| `--bind HOST` | `0.0.0.0` | HTTP 服务器监听地址 |
| `--report-only` | — | 不测试，仅重新生成报告并启动服务器 |
| `--no-serve` | — | 测试完成后不启动 HTTP 服务器 |
| `--tests 0,1,2` | 全部 | 只运行指定序号的测试项（并行测试仅在全量时运行） |

---

## 单实例测试项列表

| 序号 | 名称 | 编码器 | 分辨率 | Preset |
|------|------|--------|--------|--------|
| 0 | H.264  1080p  fast | libx264 | 1920x1080 | fast |
| 1 | H.264  1080p  medium | libx264 | 1920x1080 | medium |
| 2 | H.264  4K     fast | libx264 | 3840x2160 | fast |
| 3 | H.264  4K     medium | libx264 | 3840x2160 | medium |
| 4 | H.264  4K     threads32 | libx264 | 3840x2160 | fast |
| 5 | H.264  4K     threads64 | libx264 | 3840x2160 | fast |
| 6 | H.265  1080p  fast | libx265 | 1920x1080 | fast |
| 7 | H.265  1080p  medium | libx265 | 1920x1080 | medium |
| 8 | H.265  4K     fast | libx265 | 3840x2160 | fast |
| 9 | H.265  4K     threads32 | libx265 | 3840x2160 | fast |
| 10 | VP9    1080p  speed4 | libvpx-vp9 | 1920x1080 | N/A |

---

## 并行多实例测试项列表

| 名称 | 实例数 | 编码器 | 分辨率 | Preset |
|------|--------|--------|--------|--------|
| H.264  1080p  x2并行 | 2 | libx264 | 1920x1080 | fast |
| H.264  1080p  x4并行 | 4 | libx264 | 1920x1080 | fast |
| H.264  4K     x2并行 | 2 | libx264 | 3840x2160 | fast |
| H.265  1080p  x2并行 | 2 | libx265 | 1920x1080 | fast |

---

## 输出目录结构

```
benchmark_results/          ← 默认输出目录（脚本同级）
├── report.html             ← HTML 报告（浏览器打开）
├── result_4-channel_20250301_120000.json
├── result_2-channel_20250301_140000.json
└── result_1-channel_20250301_160000.json
```

JSON 文件中包含 `test_results`（单实例）、`parallel_results`（并行）、`mbw_result` 三部分。

---

## SSH 隧道访问说明

服务器一般没有公网 HTTP 访问权限，推荐通过 SSH 隧道访问报告：

```bash
# 在笔记本本地终端执行（保持运行）
ssh -N -L <本地端口>:<服务器IP>:<服务器端口> <用户名>@<服务器IP>

# 示例
ssh -N -L 8080:192.168.1.100:8080 root@192.168.1.100

# 然后笔记本浏览器打开
http://localhost:8080/report.html
```

脚本启动服务器时会自动打印该命令，直接复制使用即可。

---

## 报告内容说明

| 区域 | 内容 | 触发条件 |
|------|------|---------|
| 总览 | 各 Channel 配置总 FPS 对比卡片 + mbw 带宽对比表 | 始终显示 |
| 图表 | 分测试项 FPS 分组柱状图 + 总 FPS 柱状图 | 始终显示 |
| **并行测试** | 多实例合计 FPS 分组柱状图 | 有并行测试数据时 |
| **对比分析** | 多配置 FPS 对比表 + 衰减百分比 + 趋势折线图 | **≥ 2 次测试后** |
| 配置详情 | CPU / 内存 / DIMM / FFmpeg / mbw / 并行结果明细 | 始终显示 |

> **FPS 计算**：`编码帧数 ÷ 实际耗时`，比 FFmpeg 进度行瞬时值更准确。

---

## 对比分析说明

运行 ≥ 2 次（不同 `--label`）后，报告自动生成对比分析区块：

- **对比表格**：以第一次运行的配置为基准（★），显示每个测试项在各配置下的 FPS
  - 绿色 `+x.x%` = 相对基准性能提升
  - 红色 `-x.x%` = 相对基准性能下降
- **趋势折线图**：直观展示 channel 减少后总 FPS 的变化曲线
- **mbw 横向对比**：各配置内存带宽一览，与 FPS 变化相互印证

---

## 常见问题

**Q: DIMM 插槽信息显示"需 root 权限"？**
使用 `sudo python3 ffmpeg_benchmark.py ...` 运行即可。

**Q: H.265 / VP9 显示"编码器不可用"？**
检查 FFmpeg 编译时是否包含相应编码器：
```bash
ffmpeg -encoders 2>/dev/null | grep -E 'libx265|vp9'
```

**Q: mbw 不存在？**
```bash
sudo apt install mbw    # Ubuntu/Debian
sudo yum install mbw    # CentOS/RHEL
```

**Q: 端口被占用？**
用 `--port 9090` 指定其他端口，脚本也会自动尝试顺延端口。

**Q: 如何只跑部分测试节省时间？**
```bash
sudo python3 ffmpeg_benchmark.py --label "4-channel" --tests 0,1,4 --duration 30
```
注意：`--tests` 指定子集时不会运行并行测试。

**Q: 对比分析没有出现？**
需要至少运行两次（使用不同的 `--label`），每次结果保存为独立 JSON 文件后，
重新生成报告时会自动出现对比分析区块。
```bash
# 确认结果文件是否存在
ls benchmark_results/result_*.json
```

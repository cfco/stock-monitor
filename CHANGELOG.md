# 更新记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号对应 git tag。

## Unreleased

### 新增
- **美股（Alpaca 免费版）接入**：实时行情走 WebSocket（IEX feed，覆盖盘前盘后），历史 K 线与休市兜底走 REST，与 A 股卡片混排显示（US 标签）；分时/日K/周K 可切换；本地限流 180 次/分钟（数据源上限 200），断线 5→60s 指数退避自动重连；非有效数据窗口时价格兜底用最近收盘价，避免盘后宽价差异常报价
- **凭证加密存储**（`scripts/secret_store.py`）：Alpaca Key/Secret 经 Windows DPAPI 加密后保存（`api_key_enc`/`secret_enc`，绑定本机用户），Linux/WSL 自动降级为 base64 编码（非明文）；旧版明文凭证启动时自动迁移为密文
- **Ubuntu 支持**：GitHub Actions 新增 `build-linux` job（ubuntu-22.04 构建，glibc 2.35，兼容 22.04/24.04/WSL），产物 `StockMonitor-linux-x64.tar.gz` 单文件二进制；CI 在 xvfb 无显示环境下做启动冒烟测试；下载/运行说明见 README「快速开始」
- K 线悬浮图定位避让：弹出位置自动避开屏幕边缘与悬浮窗本体，不再遮挡卡片悬停

### 修复
- **双击关闭与 K 线悬浮图冲突**：K 线图（large 档）大面积悬在悬浮窗外时，双击落在桌面导致应用"关不掉"。现在 K 线图可接收鼠标——**双击 K 线图 = 关闭应用**（与双击悬浮窗等效）；鼠标移入图内保持显示、移出自动隐藏；单击不做任何动作
- **A 股分时 K 线改为当天全量**：原先只显示最近 60 根，现在显示 9:30 开盘至当前时间的全部 1 分钟数据（约 240 根；数据源不含 9:15-9:25 集合竞价时段），按窗口宽度自适应压缩绘制（蜡烛最小 1px）；日K/周K 仍为最近 60 根
- `secret_store.py` 移除未使用且在部分 Linux Python 版本会抛异常的 `import ctypes.wintypes`

### 变更
- 依赖新增 `websocket-client`、`tzdata`；CI 的两个构建 job 同步补装
- 原 README 中基于 Yahoo Finance 的美股说明移除（接口实测不可用，已改由 Alpaca 接入）

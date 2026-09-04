#!/bin/bash
# =============================================
#  ROS2 机器人端 — 一键启动所有服务
#  使用: chmod +x start_robot_ros2.sh && ./start_robot_ros2.sh
# =============================================

echo "=========================================="
echo "  智能语音导诊 — 全服务启动 (ROS2)"
echo "=========================================="

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------- 1. 启动 FastAPI 后端 ----------
echo "[1/4] 启动问答后端..."
cd "$SCRIPT_DIR"
python3 qa_server.py &
BACKEND_PID=$!
sleep 2

# ---------- 2. 启动讯飞麦克风 (ROS2, use_mode=M2) ----------
echo "[2/4] 启动讯飞麦克风 (ROS2)..."
source ~/ros2_ws/install/setup.bash   # ← 改成你的 ROS2 工作空间路径
ros2 launch wheeltec_mic_aiui aiui_chat.launch.py use_mode:=M2 &
MIC_PID=$!
sleep 3

# ---------- 3. 启动语音转发节点 (ROS2) ----------
echo "[3/4] 启动语音转发节点 (ROS2)..."
cd "$SCRIPT_DIR"
python3 voice_transfer_node_ros2.py &
VOICE_PID=$!

# ---------- 4. 启动前端 ----------
echo "[4/4] 启动 Electron 前端..."
cd "$SCRIPT_DIR/.."
npm run dev &
FRONTEND_PID=$!

echo ""
echo "=========================================="
echo "  ✅ 全部启动完成 (ROS2)"
echo "  问答后端: http://127.0.0.1:8000"
echo "  SSE 推送: http://127.0.0.1:8000/sse"
echo "=========================================="
echo "  按 Ctrl+C 停止所有服务"
echo ""

# 捕获退出信号，清理子进程
cleanup() {
    echo ""
    echo "正在停止所有服务..."
    kill $FRONTEND_PID 2>/dev/null
    kill $VOICE_PID 2>/dev/null
    kill $MIC_PID 2>/dev/null
    kill $BACKEND_PID 2>/dev/null
    echo "已停止"
    exit 0
}
trap cleanup SIGINT SIGTERM

# 等待任一子进程退出
wait

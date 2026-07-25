#!/bin/bash
# ComfyTester 状态检查 —— 区分"运行中"和"卡死"

HOST="172.17.80.1:8188"
OUTDIR="$HOME/comfy-tester/test_outputs/Krea2-低显存版"

echo "=== ComfyUI 队列 ==="
curl -s "http://$HOST/queue" | python3 -c "
import json,sys
q=json.load(sys.stdin)
r=len(q.get('queue_running',[]))
p=len(q.get('queue_pending',[]))
print(f'  运行中: {r}  排队中: {p}')
if r>0: print('  → 正在生图，正常')
elif p>0: print('  → 有排队但没在执行，异常！')
else: print('  → 队列空，测试可能已结束')
"

echo ""
echo "=== 最近输出文件 ==="
find "$OUTDIR" -name "*.png" -newer "$OUTDIR" -mmin -5 2>/dev/null | tail -3 | while read f; do
    echo "  $(stat -c '%y' "$f" 2>/dev/null | cut -d. -f1)  $(basename "$(dirname "$f")")/$(basename "$f")"
done

echo ""
echo "=== 进程状态 ==="
if pgrep -f "workflow_tester.py run" > /dev/null 2>&1; then
    echo "  测试进程: 运行中"
else
    echo "  测试进程: 已退出"
fi

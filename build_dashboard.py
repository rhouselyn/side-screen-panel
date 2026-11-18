"""从静态原型 dashboard-demo.html 生成运行版 dashboard.html。

设计意图：demo 保留完整的示例数据，方便单独打开预览版式；运行版把示例值清空为
占位符（等待后端推送真实数据），并把 demo 的内联预览脚本换成 QWebChannel 桥接 +
dashboard.js。

注意：这里一律用结构化正则按 class 定位，不要用「精确示例字符串」做 replace ——
以前那么写，demo 内容一改就集体失配，成品里残留一堆示例假数据却没人发现。
"""
from pathlib import Path
import re

source = Path(__file__).with_name("dashboard-demo.html").read_text(encoding="utf-8")

# 1) 标题与无障碍标签：运行版不再自称"静态设计原型"
source = source.replace("<title>副屏看板 · 静态设计原型</title>", "<title>副屏看板</title>")
source = source.replace('aria-label="副屏看板 · 示例数据"', 'aria-label="副屏看板"')

# 2) 内联预览脚本（dialog + <script>）整段换成运行期脚本
source, bridge_count = re.subn(
    r'<dialog aria-labelledby="result-title">.*?</script>',
    '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>\n<script src="dashboard.js"></script>',
    source, flags=re.S)
if bridge_count != 1:
    raise ValueError("预览脚本结构已变化，未生成运行页面")

# 3) 清掉示例会话行与示例重置卡（运行版由 JS 动态生成）
source = re.sub(r'\s*<li class="task".*?</li>', '', source, flags=re.S)
source = re.sub(r'(<ul class="cards"[^>]*>).*?(</ul>)', r'\1\2', source, flags=re.S)

# 4) 数值占位：保留 <small> 单位，只清空数字文本
source = re.sub(r'(<span class="value">)[^<]*(<small)', r'\1—\2', source)
source = re.sub(r'(<span class="temp">)[^<]*(<small)', r'\1—\2', source)
source = re.sub(r'(<div class="row"><span>(?:GPU|CPU|硬盘)</span><span class="quiet">)[^<]*', r'\1—', source)
source = source.replace('<label class="tun">TUN <input type="checkbox" aria-label="Clash TUN" checked>',
                        '<label class="tun" hidden>TUN <input type="checkbox" aria-label="Clash TUN">')
source = source.replace('<span class="label">今日预算剩余</span>', '<span class="label">周期额度</span>')
source = re.sub(r'(<div class="meta">)[^<]*(</div>)', r'\1等待数据\2', source)
source = re.sub(r'(<strong[^>]*>)[^<]*(</strong>)', r'\1—\2', source)   # 今日流量 / 重置卡张数
source = re.sub(r'(<span class="status">)[^<]*(</span>)', r'\1检测中\2', source)
source = re.sub(r'(<div class="egress"><span>)[^<]*(</span><span class="numeral ip">)[^<]*(</span>)',
                r'\1—\2—\3', source)

# 5) 会话区标题与空态：初始显示"不可用"，等首次数据推送
source = re.sub(r'(<span\b[^>]*\bid="task-count"[^>]*>).*?(</span>)',
                r'\1不可用\2', source, flags=re.S)
source = re.sub(r'<p class="empty"[^>]*>.*?</p>',
                '<p class="empty">会话状态暂不可用</p>', source, flags=re.S)

# 6) 进度条归零
source = re.sub(r'--fill:\d+%', '--fill:0%', source)

Path(__file__).with_name("dashboard.html").write_text(source, encoding="utf-8")

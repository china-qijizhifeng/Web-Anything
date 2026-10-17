from flask import Flask, request, jsonify, render_template_string, send_file
import sqlite3, os, io, shutil
from xml.sax.saxutils import escape

BASE = os.path.dirname(__file__)
SEED = os.path.join(BASE, 'seed.db')
DB = os.path.join(BASE, 'runtime.db')
app = Flask(__name__)

DEFAULT = "flowchart TD\n    A[Christmas] -->|Get money| B(Go shopping)\n    B --> C{Let me think}\n    C -->|One| D[Laptop]\n    C -->|Two| E[iPhone]\n    C -->|Three| F[fa:fa-car Car]"

def db():
    c = sqlite3.connect(DB)
    c.execute('CREATE TABLE IF NOT EXISTS diagram (id INTEGER PRIMARY KEY, code TEXT NOT NULL)')
    if c.execute('SELECT COUNT(*) FROM diagram').fetchone()[0] == 0:
        c.execute('INSERT INTO diagram(id,code) VALUES(1,?)', (DEFAULT,)); c.commit()
    return c

def svg_for(code):
    import re
    labels=[]
    for m in re.finditer(r'\b[A-Za-z]\s*(?:\[[^\]]+\]|\([^\)]+\)|\{[^\}]+\})', code):
        raw=m.group(0); label=re.sub(r'^[A-Za-z]\s*', '', raw).strip('[](){}')
        if label and label not in labels: labels.append(label)
    if not labels: labels=['草稿','审核','发布']
    labels=labels[:8]
    width=520; h=max(260, len(labels)*100+40)
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{h}" viewBox="0 0 {width} {h}"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#222"/></marker></defs>']
    if len(labels) >= 6:
        h=700; parts[0]=f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{h}" viewBox="0 0 {width} {h}"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#222"/></marker></defs>'
        for i,label in enumerate(labels[:3]):
            label = escape(label)
            y=20+i*105
            if i==2: parts.append(f'<polygon points="260,265 330,335 260,405 190,335" fill="#fff" stroke="#222" stroke-width="2"/><text x="260" y="340" text-anchor="middle" font-family="Arial,sans-serif" font-size="16" fill="#222">{label}</text>')
            else: parts.append(f'<rect x="190" y="{y}" width="140" height="52" rx="6" fill="#fff" stroke="#222" stroke-width="2"/><text x="260" y="{y+32}" text-anchor="middle" font-family="Arial,sans-serif" font-size="16" fill="#222">{label}</text>')
        parts += ['<line x1="260" y1="72" x2="260" y2="115" stroke="#222" stroke-width="2" marker-end="url(#arrow)"/>','<line x1="260" y1="177" x2="260" y2="265" stroke="#222" stroke-width="2" marker-end="url(#arrow)"/>']
        for j,label in enumerate(labels[3:6]):
            label = escape(label)
            x2=40+j*180; parts.append(f'<rect class="node" x="{x2}" y="500" width="140" height="52" rx="6" fill="#fff" stroke="#222" stroke-width="2"/><text x="{x2+70}" y="532" text-anchor="middle" font-family="Arial,sans-serif" font-size="16" fill="#222">{label}</text><line x1="260" y1="405" x2="{x2+70}" y2="500" stroke="#222" stroke-width="2" marker-end="url(#arrow)"/>')
        parts.append('</svg>'); return ''.join(parts)
    for i,label in enumerate(labels):
        label = escape(label)
        x=190; y=20+i*100; parts.append(f'<rect class="node" x="{x}" y="{y}" width="140" height="52" rx="6" fill="#fff" stroke="#222" stroke-width="2"/><text x="{x+70}" y="{y+32}" text-anchor="middle" font-family="Arial,sans-serif" font-size="16" fill="#222">{label}</text>')
        if i < len(labels)-1: parts.append(f'<line x1="{x+70}" y1="{y+52}" x2="{x+70}" y2="{y+94}" stroke="#222" stroke-width="2" marker-end="url(#arrow)"/>')
    parts.append('</svg>'); return ''.join(parts)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def editor(path=''):
    c=db(); code=c.execute('SELECT code FROM diagram WHERE id=1').fetchone()[0]; c.close()
    return render_template_string(TEMPLATE, code=code, diagram_svg=svg_for(code))

@app.post('/api/diagram')
def save_diagram():
    code=request.get_json(force=True).get('code','')
    c=db(); c.execute('UPDATE diagram SET code=? WHERE id=1',(code,)); c.commit(); c.close()
    return jsonify(ok=True, svg=svg_for(code))

@app.get('/export/svg')
def export_svg():
    c=db(); code=c.execute('SELECT code FROM diagram WHERE id=1').fetchone()[0]; c.close()
    data=svg_for(code).encode()
    return send_file(io.BytesIO(data), mimetype='image/svg+xml', as_attachment=True, download_name='mermaid-diagram.svg')

@app.post('/_reset')
@app.post('/api/reset')
def reset():
    if os.path.exists(SEED): shutil.copyfile(SEED, DB)
    else:
        c=sqlite3.connect(DB); c.execute('CREATE TABLE IF NOT EXISTS diagram (id INTEGER PRIMARY KEY, code TEXT NOT NULL)'); c.execute('INSERT OR REPLACE INTO diagram VALUES(1,?)',(DEFAULT,)); c.commit(); c.close()
    return jsonify(ok=True)

TEMPLATE = '''<!doctype html><html><head><meta charset="utf-8"><title>Online FlowChart & Diagrams Editor - Mermaid Live Editor</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;color:#24202f;background:#fff;overflow:hidden}.promo{height:48px;background:#f9dce8;color:#b00048;text-align:center;font-size:14px;letter-spacing:.3px;padding:9px}.promo button{margin-left:18px;background:#1e1a2e;color:white;border:0;border-radius:8px;padding:8px 14px;font-weight:600}.promo span{float:right;font-size:24px;margin:0 18px}.top{height:72px;background:#fff;color:#241f2e;display:flex;align-items:center;padding:0 24px;gap:12px;border-bottom:1px solid #eee}.hamb{font-size:20px}.brand{font-size:17px;font-weight:700;color:#bf1555;margin-right:24px;display:flex;align-items:center;gap:7px}.brand img,.top button img{width:16px;height:16px;vertical-align:middle}.top button{border:0;color:#241f2e;background:transparent;padding:9px 13px;border-radius:7px;font-size:13px}.top .primary{background:#6656c9;color:#fff}.shell{height:calc(100vh - 120px);display:grid;grid-template-columns:30.4% 69.6%;min-height:600px}.left{position:relative;border-right:1px solid #ddd;background:#fafafa;display:flex;flex-direction:column}.tabs{height:48px;background:#fff;display:flex;align-items:center;padding:0 20px;gap:4px}.tab{padding:9px 12px;border:0;background:transparent;color:#555;font-weight:600}.tab.active{background:#fff;border-bottom:2px solid #6555c7;color:#222}.tab.docs{margin-left:auto}.editor-wrap{height:445px;display:flex;background:#fff}.gutter{width:48px;background:#f4f4f4;color:#888;text-align:right;padding:14px 10px;line-height:24px;font:14px monospace}.editor{flex:1;border:0;outline:0;resize:none;padding:14px;font:14px/24px 'Droid Sans Mono',monospace;color:#222;background:white;white-space:pre;tab-size:4}.examples{height:280px;overflow:hidden;padding:14px;background:#f7f7f7;border-top:1px solid #ddd}.examples h4{margin:0 0 10px;font-size:14px}.examples h4 span{float:right}.chips{display:flex;flex-wrap:wrap;gap:7px}.chip{background:#eee;color:#222;border:0;border-radius:11px;padding:7px 10px;font-size:12px}.right{position:relative;background-color:#fff;background-image:linear-gradient(#eee 1px,transparent 1px),linear-gradient(90deg,#eee 1px,transparent 1px);background-size:20px 20px}.canvas{position:absolute;inset:48px 0 0;display:flex;align-items:center;justify-content:center}.preview{padding:0}.actions{height:44px;background:#f7f7f7;display:flex;align-items:center;justify-content:space-between;padding:0 16px;cursor:pointer;border-top:1px solid #eee}.actions span{font-weight:600}.action-menu{display:none;position:absolute;bottom:42px;left:16px;background:#fff;border:1px solid #ccc;border-radius:6px;box-shadow:0 4px 12px #999;z-index:3;padding:12px;width:220px}.action-menu.open{display:block}.action-menu button{display:block;width:100%;padding:9px;border:0;background:#fff;text-align:left;border-radius:4px}.action-menu button:hover{background:#eee}.modal{position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:10;display:flex;align-items:center;justify-content:center}.dialog{position:relative;background:#fff;border-radius:12px;width:512px;height:646px;padding:32px;box-shadow:0 8px 35px #222;text-align:left}.dialog .close{position:absolute;right:14px;top:12px;border:0;background:transparent;font-size:24px;padding:0;color:#8390a5}.logo-img{width:40px;height:40px;border-radius:8px;object-fit:contain;background:#ed0060;margin-bottom:18px}.dialog h2{margin:0 0 10px;font-size:24px;color:#101827}.dialog p{color:#8b9ab2;line-height:1.45;font-size:13px}.dialog button{padding:10px 15px;border-radius:6px;border:1px solid #d7dfeb;background:#fff;font-weight:600}.features{margin-top:18px}.features>div{height:60px;border:1px solid #d7dfeb;border-radius:12px;margin:11px 0;padding:12px;display:flex;gap:12px;font-size:13px;align-items:center}.features b{color:#ed0060;font-size:18px}.features small{display:block;color:#9aa8bd;font-size:12px;margin-top:3px}.modal-buttons{margin-top:18px}.dialog .trial{background:#ed0060;color:#fff;border-color:#ed0060;margin-right:8px}.dialog .stay{color:#192033}.trust{text-align:center;font-size:12px;line-height:2.2;margin-top:12px;color:#182033}.trust strong{font-size:18px;color:#777}.status{position:absolute;bottom:14px;right:20px;color:#666;font-size:12px}
</style></head><body><div class="promo">Try Mermaid Advanced Editor — OSS users get 10% off with code JS26 <button>Get started</button><span>×</span></div><header class="top"><div class="hamb">☰</div><div class="brand"><img src="/static/mermaidchart-logo.svg"> <span>Mermaid Live Editor</span></div><span style="flex:1"></span><button>◉</button><button>◷</button><button class="primary" id="share">Share</button><button id="contact"><img src="/static/mermaidchart-logo.svg"> Contact sales</button><button id="save"><img src="/static/mermaidchart-logo.svg"> Save diagram</button></header><main class="shell"><section class="left"><div class="tabs"><button class="tab active">▣ &nbsp;Code</button><button class="tab">⚙ &nbsp;Config</button><button class="tab docs">▣ &nbsp;Docs</button></div><div class="editor-wrap"><div class="gutter" id="gutter"></div><textarea id="editor" class="editor" aria-label="Editor content">{{ code }}</textarea></div><div class="examples"><h4>⌘ &nbsp;Sample Diagrams <span>⌄</span></h4><div class="chips"><button class="chip">Flowchart　⌄</button><button class="chip">Class　⌄</button><button class="chip">Sequence　⌄</button><button class="chip">Entity Relationship　⌄</button><button class="chip">State　⌄</button><button class="chip">Mindmap　⌄</button><button class="chip">Architecture　⌄</button><button class="chip">Block　⌄</button><button class="chip">C4　⌄</button><button class="chip">Cynefin Framework　⌄</button><button class="chip">Event Modeling　⌄</button><button class="chip">Gantt　⌄</button><button class="chip">Git　⌄</button><button class="chip">Ishikawa　⌄</button><button class="chip">Kanban　⌄</button><button class="chip">Packet　⌄</button><button class="chip">Pie　⌄</button><button class="chip">Quadrant　⌄</button><button class="chip">Radar　⌄</button><button class="chip">Railroad (ABNF)　⌄</button><button class="chip">Railroad (EBNF)　⌄</button><button class="chip">Railroad (IR)　⌄</button></div></div><div class="actions" id="actions"><span>↧ &nbsp;Actions</span><span>⌃</span></div><div class="action-menu" id="action-menu"><label style="display:block;font-size:12px;color:#666;margin-bottom:7px">PNG size</label><div style="display:flex;gap:4px;margin-bottom:8px"><button style="background:#eee">Auto</button><button>Width</button><button>Height</button></div><button id="export-svg">SVG</button><button>PNG</button><button>Kroki</button><button>Copy Image</button><button>Copy Markdown</button><button>Load Gist</button></div></section><section class="right"><div class="canvas"><div class="preview" id="preview">{{ diagram_svg|safe }}</div></div><div class="status" id="status">v12.2.0　◉　☼</div></section></main><div id="upsell" class="modal"><div class="dialog"><button class="close">×</button><img class="logo-img" src="/static/mermaidchart-logo.svg"><h2>Try the full Mermaid experience</h2><p>Free forever, with Plus features free for 7 days.</p><div class="features"><div><b>●</b><span>AI diagram generation<small>Describe what you need, AI builds it</small></span></div><div><b>●</b><span>Visual drag-and-drop editor<small>Edit diagrams without writing code</small></span></div><div><b>●</b><span>Unlimited diagram storage<small>Save and organize all your diagrams</small></span></div><div><b>●</b><span>Team collaboration<small>Share, comment, and edit together in real-time</small></span></div></div><div class="modal-buttons"><button class="trial">Start free trial</button><button class="stay" id="stay-on-mermaid">Stay on mermaid.live</button></div><div class="trust">Trusted by 5M people and over 200k companies<br><strong>Google　 Microsoft　 ▲　 aws</strong><br><u>mermaid.ai Privacy Policy</u></div></div></div><script>
const ed=document.getElementById('editor'), prev=document.getElementById('preview'), gutter=document.getElementById('gutter');
function lines(){gutter.innerHTML=ed.value.split('\\n').map((_,i)=>i+1).join('<br>')}; lines();
function update(){lines();fetch('/api/diagram',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:ed.value})}).then(r=>r.json()).then(x=>{prev.innerHTML=x.svg;document.getElementById('status').textContent='Updated · v12.2.0'})};
ed.addEventListener('input',()=>{clearTimeout(window.t);window.t=setTimeout(update,120)});
document.getElementById('stay-on-mermaid').onclick=()=>document.getElementById('upsell').style.display='none';
document.getElementById('actions').onclick=()=>document.getElementById('action-menu').classList.toggle('open');
document.getElementById('export-svg').onclick=()=>{document.getElementById('status').textContent='SVG exported';window.location='/export/svg'};
</script></body></html>'''

if __name__=='__main__':
    db(); app.run(host='127.0.0.1', port=int(os.environ.get('PORT',5000)), debug=False)

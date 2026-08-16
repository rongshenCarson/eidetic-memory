#!/usr/bin/env python3
"""#46 空间漫游 UI（对标 MemPalace traverse）：实体 → 关系 → 关联记忆 的可视化页面

用法: eidetic traverse <实体> [--depth 2] [--out /tmp/traverse.html]
生成 HTML（实体卡 + 关系网 + 关联记忆），浏览器打开即可漫游。
"""
import sys
import html
import time


def _collect(entity, depth=2, limit=30):
    """BFS 收集实体关系网络"""
    from memory_server import db
    conn = db.get_conn()
    try:
        nodes = {}   # name -> {type, relations: [(pred, other, dir)], chunks: []}
        frontier = [entity]
        seen = set()
        for _ in range(depth):
            nxt = []
            for ent in frontier:
                if ent in seen:
                    continue
                seen.add(ent)
                rels = conn.execute(
                    "SELECT subject, predicate, object FROM triples "
                    "WHERE subject=? OR object=?", (ent, ent)).fetchall()
                if not rels:
                    continue
                nodes[ent] = {"relations": [], "chunks": []}
                for r in rels:
                    other = r["object"] if r["subject"] == ent else r["subject"]
                    nodes[ent]["relations"].append((r["predicate"], other, "→" if r["subject"] == ent else "←"))
                    if other not in seen:
                        nxt.append(other)
                for c in conn.execute(
                        "SELECT id, namespace, substr(text,1,100) t FROM chunks "
                        "WHERE text LIKE ? LIMIT 2", (f"%{ent}%",)).fetchall():
                    nodes[ent]["chunks"].append((c["namespace"], c["t"]))
                if len(seen) >= limit:
                    break
            frontier = [x for x in nxt if x not in seen][:limit]
            if not frontier:
                break
        return nodes
    finally:
        conn.close()


def render(entity, depth=2, limit=30):
    nodes = _collect(entity, depth, limit)
    cards = []
    for name, info in nodes.items():
        rels = "".join(
            f'<div class="rel"><span class="pred">{html.escape(p)}</span> '
            f'<span class="dir">{d}</span> <a href="#{html.escape(o)}">{html.escape(o)}</a></div>'
            for p, o, d in info["relations"][:12])
        chunks = "".join(
            f'<div class="chunk"><span class="ns">{html.escape(ns)}</span> {html.escape(t)}...</div>'
            for ns, t in info["chunks"][:2])
        cards.append(f'''
        <div class="card" id="{html.escape(name)}">
          <h3>{html.escape(name)}</h3>
          <div class="rels">{rels or '<div class="muted">无关联</div>'}</div>
          <div class="chunks">{chunks}</div>
        </div>''')
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Eidetic 空间漫游: {html.escape(entity)}</title>
<style>
body{{font-family:-apple-system,sans-serif;background:#f5f4f0;color:#222;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 16px}} h3{{margin:0 0 8px;font-size:15px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}}
.card{{background:#fff;border:1px solid #e2ddd2;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.rel{{font-size:13px;margin:3px 0;color:#444}} .pred{{color:#8a6d3b;font-weight:600}}
.dir{{color:#999}} .chunk{{font-size:12px;color:#666;background:#faf8f4;border-radius:6px;padding:6px;margin-top:6px}}
.ns{{display:inline-block;background:#eef3ea;color:#4a6b3a;border-radius:4px;padding:0 5px;font-size:11px;margin-right:4px}}
.muted{{color:#aaa;font-size:12px}} a{{color:#2b5f8a;text-decoration:none}} a:hover{{text-decoration:underline}}
.toolbar{{margin-bottom:12px}} input{{padding:6px 10px;border:1px solid #ccc;border-radius:6px;width:240px}}
button{{padding:6px 12px;border:0;background:#4a6b3a;color:#fff;border-radius:6px;cursor:pointer}}
</style></head><body>
<h1>🧭 Eidetic 空间漫游 — {html.escape(entity)}</h1>
<div class="toolbar"><input id="q" placeholder="输入实体名回车跳转"><button onclick="location.hash='#'+document.getElementById('q').value">跳转</button></div>
<div class="cards">{''.join(cards)}</div>
</body></html>"""


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="eidetic traverse", description="空间漫游 UI（生成 HTML）")
    ap.add_argument("entity")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--out", default="/tmp/eidetic_traverse.html")
    args = ap.parse_args(argv)
    html_out = render(args.entity, args.depth)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"✅ 漫游页已生成: {args.out}（浏览器打开即可）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

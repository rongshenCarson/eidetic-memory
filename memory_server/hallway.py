#!/usr/bin/env python3
"""#45 hallway 漫游：从实体出发沿 KG 关联漫游（跨域，对标 MemPalace hallway/tunnel）

用法: eidetic hallway <实体> [--depth 2] [--limit 20]
返回: 实体 → 关联实体（KG 一跳/多跳）→ 每实体关联的 chunk（可溯源）
"""
import sys
import time


def hallway(entity, depth=2, limit=20, namespace=None):
    """KG 多跳漫游：返回 {entity: {relations: [...], chunks: [...]}} 层级"""
    from memory_server import db
    conn = db.get_conn()
    try:
        visited = set()
        frontier = [entity]
        layers = []
        for d in range(depth):
            layer = {}
            next_frontier = []
            for ent in frontier:
                if ent in visited:
                    continue
                visited.add(ent)
                # 该实体的关系（出 + 入）
                rels = conn.execute(
                    "SELECT subject, predicate, object FROM triples "
                    "WHERE (subject=? OR object=?) AND namespace=IFNULL(?, 'default')",
                    (ent, ent, namespace)).fetchall()
                if not rels:
                    continue
                neighbors = []
                for r in rels:
                    other = r["object"] if r["subject"] == ent else r["subject"]
                    neighbors.append({"other": other, "predicate": r["predicate"], "dir": "out" if r["subject"] == ent else "in"})
                    next_frontier.append(other)
                # 关联 chunk（文本中包含实体名的记忆片段）
                chunks = conn.execute(
                    "SELECT id, namespace, substr(text,1,120) t FROM chunks "
                    "WHERE text LIKE ? LIMIT 3",
                    (f"%{ent}%",)).fetchall()
                layer[ent] = {
                    "relations": neighbors,
                    "chunks": [dict(c) for c in chunks],
                }
            if layer:
                layers.append(layer)
            frontier = [x for x in next_frontier if x not in visited][:limit]
            if not frontier:
                break
        return layers
    finally:
        conn.close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="eidetic hallway", description="KG 实体关联漫游")
    ap.add_argument("entity", help="起始实体")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--ns", default=None)
    args = ap.parse_args(argv)

    layers = hallway(args.entity, depth=args.depth, limit=args.limit, namespace=args.ns)
    if not layers:
        print(f"「{args.entity}」无 KG 关联（可检查实体名或 KG 数据）")
        return 0
    for d, layer in enumerate(layers, 1):
        print(f"=== 第 {d} 跳 ===")
        for ent, info in layer.items():
            rels = ", ".join(f"{r['other']}[{r['predicate']}]" for r in info["relations"][:6])
            print(f"  🧭 {ent} → {rels}")
            for c in info["chunks"][:2]:
                print(f"      📎 [{c['namespace']}] {c['t']}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())

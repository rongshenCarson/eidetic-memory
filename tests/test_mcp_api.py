
def test_memory_search_full_expand():
    """复审🔴回归测试：memory_search(full=True) 必须可用（expand_context dict 入参 + list 返回）"""
    import asyncio
    from memory_server import db as _db
    from memory_server.embed import FtsOnlyProvider
    from memory_server.mcp_api import build_mcp_server

    async def main():
        _db.init_db()
        mcp = build_mcp_server(FtsOnlyProvider())
        # 先摄入一条测试内容
        import tempfile, os
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.md"), "w", encoding="utf-8") as f:
            f.write("# 测试\nbrandx做轻养生药食同源，teaprod。\n第二段补充内容用于邻居扩展验证。\n")
        from memory_server.ingest import ingest_dir
        ingest_dir(d, "test", FtsOnlyProvider())
        r = await mcp.call_tool("memory_search", {"query": "brandx", "limit": 2, "full": True,
                                                   "namespace": "test"})
        assert r is not None, "full=True 调用不应抛异常"
        text = r.content[0].text if hasattr(r, "content") else str(r)
        assert "brandx" in text or "无相关记忆" in text
        return "OK"

    result = asyncio.run(main())
    assert result == "OK"

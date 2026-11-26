"""多用户隔离回归测试。

覆盖验收标准里的越权路径：私有文件夹、会话、消息、工具调用记录、炼化接口。
两个用户通过 ``as_user`` 切换真实身份（而不是前端隐藏），所有断言都打在
服务端响应上。
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import func, select

from api.models.conversation import Conversation, Message


async def _make_conversation(db, project_id: str, user_id: str, title: str) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        user_id=user_id,
        title=title,
        status="active",
        token_budget=128000,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _add_turn(db, conv: Conversation, *, user_text: str, tool: str | None = None,
                    tool_output: dict | None = None) -> None:
    seq_row = await db.execute(
        select(func.coalesce(func.max(Message.sequence_num), 0)).where(
            Message.conversation_id == conv.conversation_id
        )
    )
    start = int(seq_row.scalar_one() or 0)
    db.add(Message(
        conversation_id=conv.conversation_id,
        role="user",
        content=user_text,
        sequence_num=start + 1,
    ))
    tool_calls = None
    if tool:
        tool_calls = json.dumps([{
            "callId": f"c-{uuid.uuid4().hex[:6]}",
            "tool": tool,
            "input": {"path": "report.md"},
            "output": tool_output or {"ok": True},
            "status": "done",
        }])
    db.add(Message(
        conversation_id=conv.conversation_id,
        role="assistant",
        content="已完成。",
        tool_calls=tool_calls,
        sequence_num=start + 2,
    ))
    await db.commit()


async def test_private_project_is_invisible_and_unreachable(client, as_user, make_project, db):
    project = await make_project(created_by="user-a", visibility="private")
    await _make_conversation(db, project.project_id, "user-a", "A 的私有会话")

    async with as_user("user-b"):
        listing = await client.get("/api/projects")
        assert listing.status_code == 200
        assert project.project_id not in {p["project_id"] for p in listing.json()}

        # 直接猜 id 也拿不到
        detail = await client.get(f"/api/projects/{project.project_id}")
        assert detail.status_code == 403

        # 公共/私有属性只有创建者能改
        patch = await client.patch(
            f"/api/projects/{project.project_id}", json={"visibility": "public"}
        )
        assert patch.status_code == 403


async def test_public_project_shared_but_conversations_stay_private(
    client, as_user, make_project, db
):
    project = await make_project(created_by="user-a", visibility="public")
    conv = await _make_conversation(db, project.project_id, "user-a", "A 的会话")
    await _add_turn(db, conv, user_text="帮我整理季度报表", tool="filesystem")

    async with as_user("user-b"):
        # 公共文件夹可见、可加载
        assert (await client.get(f"/api/projects/{project.project_id}")).status_code == 200
        # 但里面的会话列表为空，且按 id 读取返回 404（不是 403，避免存在性泄露）
        convs = await client.get(f"/api/projects/{project.project_id}/conversations")
        assert convs.status_code == 200
        assert convs.json() == []
        assert (await client.get(f"/api/conversations/{conv.conversation_id}/messages")).status_code == 404
        assert (await client.get(f"/api/conversations/{conv.conversation_id}/tasks")).status_code == 404


async def test_project_payload_hides_internal_fields(client, as_user, make_project):
    project = await make_project(created_by="user-a", visibility="public")
    async with as_user("user-a"):
        data = (await client.get(f"/api/projects/{project.project_id}")).json()
    assert "fs_path" not in data
    assert "created_by" not in data
    assert data["is_owner"] is True


async def test_distill_sources_only_own_conversations(client, as_user, make_project, db):
    project = await make_project(created_by="user-a", visibility="public")
    a_conv = await _make_conversation(db, project.project_id, "user-a", "A 的会话")
    await _add_turn(db, a_conv, user_text="A 的问题", tool="filesystem")

    async with as_user("user-b"):
        sources = (await client.get("/api/skills/distill/sources")).json()
        assert sources["total"] == 0
        assert sources["items"] == []

    async with as_user("user-a"):
        sources = (await client.get("/api/skills/distill/sources")).json()
        # 同一会话库里其它用例也会给 user-a 建会话，这里只断言"自己那条"可见，
        # 不用绝对条数（否则测试顺序一变就红）。
        ids = [item["conversation_id"] for item in sources["items"]]
        assert a_conv.conversation_id in ids
        own = next(
            item for item in sources["items"]
            if item["conversation_id"] == a_conv.conversation_id
        )
        assert own["tool_call_count"] == 1


async def test_distill_rejects_foreign_conversation_id(client, as_user, make_project, db):
    project = await make_project(created_by="user-a", visibility="public")
    a_conv = await _make_conversation(db, project.project_id, "user-a", "A 的会话")
    await _add_turn(db, a_conv, user_text="A 的问题")

    async with as_user("user-b"):
        resp = await client.post(
            "/api/skills/distill/generate",
            json={"conversation_ids": [a_conv.conversation_id]},
        )
        # 404：不确认该 id 是否存在
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "DISTILL_SOURCE_NOT_FOUND"


async def test_distill_generates_skill_and_redacts_secrets(client, as_user, make_project, db):
    project = await make_project(created_by="user-a", visibility="public")
    conv = await _make_conversation(db, project.project_id, "user-a", "季度报表自动化")
    await _add_turn(
        db, conv,
        user_text="用 sk-abcdef1234567890 调用接口拉数据",
        tool="api_request",
        tool_output={"error": "401 at C:\\Users\\alice\\secret\\token.txt"},
    )

    async with as_user("user-a"):
        resp = await client.post(
            "/api/skills/distill/generate",
            json={"conversation_ids": [conv.conversation_id]},
        )
        assert resp.status_code == 200
        body = resp.json()
        md = body["markdown"]
        assert body["stats"]["conversations"] == 1
        assert body["stats"]["tool_calls"] == 1
        assert "api_request" in md
        # 凭证与本机路径不得出现在产物里
        assert "sk-abcdef1234567890" not in md
        assert "C:\\Users\\alice" not in md
        assert "<本地路径>" in md


async def test_distill_requires_a_source(client, as_user):
    async with as_user("user-a"):
        resp = await client.post("/api/skills/distill/generate", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "DISTILL_NO_SOURCE"

"""升压站母线状态机、送电去重、调度联动、落盘持久化回归测试。"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def post(client, path, values):
    body = client.post(path, json={"values": values}).json()
    return body


def test_list_and_detail_share_same_source(client):
    """列表与详情页的母线/断路器状态必须一致。"""
    rows = client.get("/api/boosting_station").json()["items"]
    for row in rows:
        detail = client.get(f"/api/boosting_station/{row['id']}").json()
        assert detail["母线状态"] == row["母线状态"]
        assert detail["断路器状态"] == row["断路器状态"]
        assert detail["运行状态"] == row["母线状态"]
    # 运行必合闸，非运行必分闸
    assert {r["母线状态"]: r["断路器状态"] for r in rows}["运行"] == "合闸"


def test_cross_step_and_backward_are_blocked(client):
    # 热备用回退到运行：拦（必须走送电）
    body = post(client, "/api/boosting_station/2/bus-state", {"target": "运行"})
    assert body["ok"] is False and "送电" in body["message"]
    # 热备用跨档到停运：拦
    body = post(client, "/api/boosting_station/2/bus-state", {"target": "停运"})
    assert body["ok"] is False and "逐档" in body["message"]
    # 运行跨档到检修（跳过热备用）：拦
    body = post(client, "/api/boosting_station/1/bus-state", {"target": "检修"})
    assert body["ok"] is False and "逐档" in body["message"]
    # 非法状态名：拦
    body = post(client, "/api/boosting_station/2/bus-state", {"target": "啥也不是"})
    assert body["ok"] is False


def test_forward_one_step_allowed(client):
    body = post(client, "/api/boosting_station/2/bus-state", {"target": "检修"})
    assert body["ok"] and body["entry"]["母线状态"] == "检修"
    assert body["entry"]["断路器状态"] == "分闸"


def test_breaker_open_blocks_energize(client):
    body = post(client, "/api/boosting_station/2/energize", {"breaker_closed": False})
    assert body["ok"] is False and "断路器尚未合闸" in body["message"]
    detail = client.get("/api/boosting_station/2").json()
    assert detail["母线状态"] == "热备用" and detail["断路器状态"] == "分闸"


def test_shutdown_station_cannot_energize(client):
    body = post(client, "/api/boosting_station/3/energize", {"breaker_closed": True})
    assert body["ok"] is False and "停运" in body["message"]


def test_maintenance_station_cannot_energize(client):
    post(client, "/api/boosting_station/2/bus-state", {"target": "检修"})
    body = post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    assert body["ok"] is False and "检修" in body["message"]


def test_energize_success_syncs_one_dispatch_order(client):
    before = client.get("/api/dispatch?status=待执行").json()["total"]
    body = post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    assert body["ok"]
    assert body["entry"]["母线状态"] == "运行"
    assert body["entry"]["断路器状态"] == "合闸"
    after = client.get("/api/dispatch?status=待执行").json()["total"]
    assert after == before + 1
    orders = [
        o for o in client.get("/api/dispatch").json()["items"]
        if o.get("关联升压站") == "BOOS-0002" and o["指令类型"] == "站内送电"
    ]
    assert len(orders) == 1 and orders[0]["status"] == "待执行"


def test_duplicate_energize_creates_only_one_order(client):
    post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    body = post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    assert body["ok"] and "重复" in body["message"]
    orders = [
        o for o in client.get("/api/dispatch").json()["items"]
        if o.get("关联升压站") == "BOOS-0002" and o["指令类型"] == "站内送电"
    ]
    assert len(orders) == 1


def test_old_order_closed_after_leaving_running_then_reenergize(client):
    post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    body = post(client, "/api/boosting_station/2/bus-state", {"target": "热备用"})
    assert body["ok"]
    orders = [
        o for o in client.get("/api/dispatch").json()["items"]
        if o.get("关联升压站") == "BOOS-0002" and o["指令类型"] == "站内送电"
    ]
    assert orders and orders[0]["status"] == "已完成"
    # 再送可以重新落一条
    post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    orders = [
        o for o in client.get("/api/dispatch").json()["items"]
        if o.get("关联升压站") == "BOOS-0002" and o["指令类型"] == "站内送电"
    ]
    assert len(orders) == 2 and orders[-1]["status"] == "待执行"


def test_walk_to_shutdown_terminal_state(client):
    post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    for target in ["热备用", "检修", "停运"]:
        body = post(client, f"/api/boosting_station/2/bus-state", {"target": target})
        assert body["ok"] and body["entry"]["母线状态"] == target
    body = post(client, "/api/boosting_station/2/bus-state", {"target": "检修"})
    assert body["ok"] is False and "末态" in body["message"]


def test_bus_state_changes_create_dispatch_orders(client):
    before = client.get("/api/dispatch?status=待执行").json()["total"]
    body = post(client, "/api/boosting_station/2/bus-state", {"target": "检修"})
    assert body["ok"]
    after = client.get("/api/dispatch?status=待执行").json()["total"]
    assert after == before + 1


def test_legacy_actions_keep_old_conclusions(client):
    created = client.post(
        "/api/boosting_station",
        json={"values": {"升压站编号": "BOOS-T", "进线电压": "35kV", "出线电压": "110kV"}},
    ).json()
    nid = created["entry"]["id"]
    # 老「恢复正常」未确认合闸：拦
    body = post(client, f"/api/boosting_station/{nid}/actions", {"action": "恢复正常"})
    assert body["ok"] is False and "断路器" in body["message"]
    # 带上合闸标记：成功转运行
    body = post(client, f"/api/boosting_station/{nid}/actions", {"action": "恢复正常", "断路器状态": "合闸"})
    assert body["ok"] and body["entry"]["母线状态"] == "运行"
    # 检查保护→热备用、安排检修→检修
    body = post(client, f"/api/boosting_station/{nid}/actions", {"action": "检查保护"})
    assert body["ok"] and body["entry"]["母线状态"] == "热备用"
    body = post(client, f"/api/boosting_station/{nid}/actions", {"action": "安排检修"})
    assert body["ok"] and body["entry"]["母线状态"] == "检修"
    # 非法动作
    body = post(client, f"/api/boosting_station/{nid}/actions", {"action": "乱按"})
    assert body["ok"] is False


def test_dispatch_legacy_actions_unchanged(client):
    oid = client.get("/api/dispatch?status=待执行").json()["items"][0]["id"]
    body = post(client, f"/api/dispatch/{oid}/actions", {"action": "确认执行"})
    assert body["ok"] and body["entry"]["status"] == "执行中"
    body = post(client, f"/api/dispatch/{oid}/actions", {"action": "完成回复"})
    assert body["ok"] and body["entry"]["status"] == "已完成"


def test_export_is_ordered_and_consistent(client):
    exp = client.get("/api/boosting_station/export").json()
    assert exp["total"] == len(exp["items"])
    assert [it["id"] for it in exp["items"]] == sorted(it["id"] for it in exp["items"])
    for it in exp["items"]:
        assert set(["id", "升压站编号", "母线状态", "断路器状态", "运行状态"]).issubset(it)
        if it["母线状态"] == "运行":
            assert it["断路器状态"] == "合闸"
        else:
            assert it["断路器状态"] == "分闸"


def test_state_persists_across_restart(client, tmp_path):
    post(client, "/api/boosting_station/2/energize", {"breaker_closed": True})
    for target in ["热备用", "检修", "停运"]:
        post(client, "/api/boosting_station/2/bus-state", {"target": target})

    # 重新实例化 Store（走同一个临时落盘文件），模拟进程重启
    from app import store as store_module

    fresh_store = store_module.Store()
    row = next(r for r in fresh_store.rows("boosting_station") if r["id"] == 2)
    assert row["母线状态"] == "停运"
    assert row["断路器状态"] == "分闸"

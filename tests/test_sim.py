"""模拟器：运动学、碰撞、测距、渲染。没有硬件时它就是唯一的"真机"。"""
from __future__ import annotations

from app import sim as simmod


def test_map_is_rectangular_and_spawn_free():
    widths = {len(row) for row in simmod.MAP}
    assert len(widths) == 1, "地图每行长度必须一致"
    assert simmod.char_at(0.90, 3.45) == ".", "出生点不能落在墙里"


def test_forward_moves_along_heading():
    s = simmod.Sim(160, 120)
    x0 = s.state.x
    result = s.step("forward", 0.5, 1.0)
    assert s.state.x > x0
    assert abs(s.state.y - 3.45) < 1e-6
    assert result["moved_m"] > 0.1


def test_turn_changes_heading_only():
    s = simmod.Sim(160, 120)
    x0, y0 = s.state.x, s.state.y
    s.step("turn_left", 0.5, 1.0)
    assert abs(s.state.x - x0) < 1e-6 and abs(s.state.y - y0) < 1e-6
    assert s.state.theta != 0.0
    s.step("turn_right", 0.5, 1.0)
    assert abs(s.state.theta % 6.2832) < 0.05


def test_wall_blocks_and_counts_bump():
    s = simmod.Sim(160, 120)
    s.state.x = 0.55  # 贴着西墙
    s.state.theta = 3.14159  # 朝西
    s.step("forward", 1.0, 1.0)
    assert s.state.x > 0.3, "不该穿墙"
    assert s.state.bump >= 1


def test_front_distance_sees_person():
    s = simmod.Sim(160, 120)
    d = s.front_distance()
    assert 0.5 < d < 3.0


def test_render_is_jpeg():
    s = simmod.Sim(160, 120)
    jpeg = s.render()
    assert jpeg.startswith(b"\xff\xd8\xff"), "应该是 JPEG 头"
    assert len(jpeg) > 2000


def test_map_render_is_png():
    s = simmod.Sim(160, 120)
    png = s.render_map(scale=8)
    assert png.startswith(b"\x89PNG")


def test_snapshot_has_expected_fields():
    snap = simmod.Sim(160, 120).snapshot()
    for key in ("x", "y", "heading_deg", "battery", "front_m", "sprites"):
        assert key in snap


def test_reset_restores_spawn():
    s = simmod.Sim(160, 120)
    s.step("forward", 1.0, 1.0)
    s.reset()
    assert (s.state.x, s.state.y) == (0.90, 3.45)

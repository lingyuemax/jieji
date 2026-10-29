"""模拟器：没有硬件也能把「看 → 想 → 动」跑完整。

一台差速小车 + 一间有走廊、箱子、人、红杯子和门牌的屋子。
摄像头画面是真算出来的：逐列光线投射（ray casting）画墙，逐行地面投射画地砖，
再按深度把人和杯子当广告牌贴上去。所以「离墙多近」「是不是正对着人」
在像素上是连续变化的 —— 大模型看到的是这个，决策才有意义。

渲染参数按真实尺度来：房间 6.6m × 4.8m，墙高 2.4m，摄像头离地 0.32m，
垂直焦距 f≈0.9H。所以一堵 1m 外的墙会占掉大半个画面，和真摄像头一致。
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

CELL = 0.30          # 一格 30cm
WALL_CHARS = {"#", "B"}
FOV = math.radians(66)
MAX_DIST = 9.0
WALL_H = 2.4         # 墙高（米）
CAM_H = 0.32         # 摄像头离地（米）
TILE = 0.60          # 地砖边长（米）

# 6.6m × 4.8m：北边三间房，中间一条东西向走廊（1.8m 宽），南边空地
MAP: tuple[str, ...] = (
    "########################",
    "#......#.........#.....#",
    "#......#....T....#..B..#",
    "#..B...#.........#.....#",
    "#......#.........#.....#",
    "#......#....#....#.....#",
    "#......#....#....#.....#",
    "#......#.........#.....#",
    "####.####.........####.#",
    "#......................#",
    "#......................#",
    "#......................#",
    "#...........B..........#",
    "#......................#",
    "#......................#",
    "########################",
)

WALL_COLORS = {
    "#": (124, 122, 116),   # 水泥墙：中性灰，避免被模型说成"蓝色面板"
    "B": (150, 108, 66),    # 纸箱
}
FLOOR_COLOR = (92, 89, 84)
FLOOR_ALT = (82, 80, 76)
CEIL_COLOR = (34, 35, 38)


@dataclass
class Sprite:
    name: str
    x: float
    y: float
    width: float = 0.45     # 米
    height: float = 0.85    # 米
    color: tuple[int, int, int] = (200, 80, 80)
    shape: str = "box"      # box | person | sign
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "x": round(self.x, 2), "y": round(self.y, 2), "shape": self.shape}


@dataclass
class SimState:
    x: float = 0.90
    y: float = 3.45
    theta: float = 0.0
    speed: float = 0.0
    battery: float = 92.0
    bump: int = 0
    distance_m: float = 0.0
    odometer: float = 0.0
    last_event: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "heading_deg": round(math.degrees(self.theta) % 360, 1),
            "speed": round(self.speed, 2),
            "battery": round(self.battery, 1),
            "bump": self.bump,
            "front_m": round(self.distance_m, 2),
            "odometer_m": round(self.odometer, 2),
            "last_event": self.last_event,
        }


def char_at(x: float, y: float) -> str:
    col = int(x / CELL)
    row = int(y / CELL)
    if row < 0 or row >= len(MAP) or col < 0 or col >= len(MAP[0]):
        return "#"
    return MAP[row][col]


def is_wall(x: float, y: float) -> bool:
    return char_at(x, y) in WALL_CHARS


class Sim:
    def __init__(self, width: int = 384, height: int = 288) -> None:
        self.width = width
        self.height = height
        self.state = SimState()
        self.sprites = [
            Sprite("红杯子", 3.75, 0.75, width=0.20, height=0.22, color=(206, 58, 52), shape="box"),
            Sprite("路人", 2.55, 3.35, width=0.50, height=1.10, color=(64, 96, 168), shape="person"),
            Sprite("门牌", 5.40, 2.42, width=0.80, height=0.26, color=(236, 234, 224), shape="sign", text="LAB 302"),
        ]
        self._z = np.full(self.width, MAX_DIST, dtype=np.float32)
        self.events: list[str] = []
        self.state.distance_m = self.front_distance()

    # ------------------------------------------------------------ 运动
    def reset(self) -> None:
        self.state = SimState()
        self.events.clear()

    def step(self, action: str, speed: float, seconds: float, dt: float = 0.08) -> dict[str, Any]:
        speed = max(0.0, min(1.0, float(speed)))
        seconds = max(0.0, min(3.0, float(seconds)))
        st = self.state
        v_max = 0.55   # m/s @ speed=1
        w_max = 2.0    # rad/s @ speed=1

        v = w = 0.0
        if action == "forward":
            v = speed * v_max
        elif action == "backward":
            v = -speed * v_max
        elif action == "turn_left":
            w = -speed * w_max
        elif action == "turn_right":
            w = speed * w_max

        t = 0.0
        moved = 0.0
        bumped = False
        while t < seconds:
            h = min(dt, seconds - t)
            nx = st.x + math.cos(st.theta) * v * h
            ny = st.y + math.sin(st.theta) * v * h
            if not self._blocked(nx, ny):
                moved += math.hypot(nx - st.x, ny - st.y)
                st.x, st.y = nx, ny
            elif not self._blocked(nx, st.y):
                moved += abs(nx - st.x)
                st.x = nx
                bumped = True
            elif not self._blocked(st.x, ny):
                moved += abs(ny - st.y)
                st.y = ny
                bumped = True
            else:
                bumped = True
            st.theta = (st.theta + w * h) % (2 * math.pi)
            t += h

        st.speed = abs(v)
        st.odometer += moved
        st.battery = max(0.0, st.battery - moved * 0.08 - seconds * 0.01)
        st.distance_m = self.front_distance()
        expected = abs(v) * seconds
        if bumped and expected > 0.01 and moved < expected * 0.5:
            # 想走 1 米只走了 0.2 米 → 确实顶住了
            st.bump += 1
            st.last_event = "被挡住了"
            self.events.append("bump")
        elif moved > 0.02:
            st.last_event = f"移动了 {moved:.2f}m"
        else:
            st.last_event = "原地"
        return {
            "moved_m": round(moved, 3),
            "bumped": bumped,
            "front_m": round(st.distance_m, 2),
            "state": st.to_dict(),
        }

    def _blocked(self, x: float, y: float) -> bool:
        r = 0.15
        for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r), (r * 0.7, r * 0.7), (-r * 0.7, -r * 0.7)):
            if is_wall(x + dx, y + dy):
                return True
        return False

    # ------------------------------------------------------------ 感知
    def front_distance(self, spread_deg: float = 18.0) -> float:
        """正前方扇区内最近障碍的距离（模拟超声波/防撞条）。

        墙、箱子、人、杯子都算 —— 真实超声波不会只认墙。
        """
        best = MAX_DIST
        for i in range(-4, 5):
            ang = self.state.theta + math.radians(spread_deg) * i / 4.0
            d, _ = self._ray(self.state.x, self.state.y, math.cos(ang), math.sin(ang))
            best = min(best, d)
        for sprite in self.sprites:
            dx, dy = sprite.x - self.state.x, sprite.y - self.state.y
            dist = math.hypot(dx, dy)
            if dist < 1e-6:
                continue
            diff = abs((math.atan2(dy, dx) - self.state.theta + math.pi) % (2 * math.pi) - math.pi)
            if math.degrees(diff) <= spread_deg:
                best = min(best, max(0.05, dist - sprite.width / 2))
        return best

    def _ray(self, px: float, py: float, rx: float, ry: float) -> tuple[float, str]:
        """DDA 网格射线，返回 (距离米, 命中格子的字符)。"""
        map_x = int(px / CELL)
        map_y = int(py / CELL)
        dx = abs(1.0 / (rx if abs(rx) > 1e-9 else 1e-9))
        dy = abs(1.0 / (ry if abs(ry) > 1e-9 else 1e-9))
        if rx < 0:
            step_x, side_x = -1, (px - map_x * CELL) / CELL * dx
        else:
            step_x, side_x = 1, ((map_x + 1) * CELL - px) / CELL * dx
        if ry < 0:
            step_y, side_y = -1, (py - map_y * CELL) / CELL * dy
        else:
            step_y, side_y = 1, ((map_y + 1) * CELL - py) / CELL * dy

        for _ in range(512):
            if side_x < side_y:
                side_x += dx
                map_x += step_x
                hit_side = 0
            else:
                side_y += dy
                map_y += step_y
                hit_side = 1
            if map_y < 0 or map_y >= len(MAP) or map_x < 0 or map_x >= len(MAP[0]):
                return MAX_DIST, "#"
            ch = MAP[map_y][map_x]
            if ch in WALL_CHARS:
                dist = (side_x - dx) if hit_side == 0 else (side_y - dy)
                return max(0.02, dist * CELL), ch
        return MAX_DIST, "#"

    def _blocked_ray(self, angle_off: float, width: int, plane_len: float) -> tuple[float, str, float]:
        """给一个屏幕列算射线：返回 (距离, 墙字符, 命中点在墙上的横向比例)。"""
        cam_x = (2 * (angle_off + width / 2) / width - 1) * plane_len
        dx, dy = math.cos(self.state.theta), math.sin(self.state.theta)
        rx = dx - dy * cam_x
        ry = dy + dx * cam_x
        return self._ray(self.state.x, self.state.y, rx, ry)

    # ------------------------------------------------------------ 渲染
    def render(self) -> bytes:
        """第一视角 → JPEG。就是发给大模型的那张图。"""
        from PIL import Image, ImageDraw

        W, H = self.width, self.height
        horizon = int(H * 0.5)
        f = H * 0.9                       # 垂直焦距（像素）
        plane_len = math.tan(FOV / 2)

        img = Image.new("RGB", (W, H), CEIL_COLOR)
        draw = ImageDraw.Draw(img)

        # ---- 天花板：上半部分做暗渐变
        for y in range(horizon):
            k = y / max(1, horizon)
            draw.line([(0, y), (W, y)], fill=tuple(int(c * (0.55 + 0.45 * k)) for c in CEIL_COLOR))

        # ---- 地面：逐行投射，铺 60cm 地砖
        dir_x, dir_y = math.cos(self.state.theta), math.sin(self.state.theta)
        plane_x, plane_y = -dir_y * plane_len, dir_x * plane_len
        rows = np.arange(horizon + 1, H, dtype=np.float32)
        depth = (CAM_H * f) / np.maximum(1.0, rows - horizon)          # (R,)
        cam_x = (np.arange(W, dtype=np.float32) / W * 2 - 1)          # (W,)
        rx = dir_x + plane_x * cam_x
        ry = dir_y + plane_y * cam_x
        wx = self.state.x + np.outer(depth, rx)
        wy = self.state.y + np.outer(depth, ry)
        checker = ((np.floor(wx / TILE) + np.floor(wy / TILE)).astype(np.int64) % 2).astype(bool)
        fade = np.clip(1.15 - depth / MAX_DIST * 0.95, 0.18, 1.0)[:, None]
        base = np.where(checker[..., None], np.array(FLOOR_COLOR), np.array(FLOOR_ALT))
        floor = (base * fade[..., None]).astype(np.uint8)
        img.paste(Image.fromarray(floor, mode="RGB"), (0, horizon + 1))

        # ---- 墙：逐列投射，按真实高度投影，带竖向接缝
        for col in range(W):
            cam = (2 * col / W - 1) * plane_len
            rrx = dir_x - dir_y * cam
            rry = dir_y + dir_x * cam
            dist, ch = self._ray(self.state.x, self.state.y, rrx, rry)
            self._z[col] = dist
            if dist >= MAX_DIST:
                continue
            top = horizon - (WALL_H - CAM_H) * f / dist
            bottom = horizon + CAM_H * f / dist
            shade = max(0.30, min(1.0, 1.0 / (1.0 + dist * 0.30)))
            side = 0.86 if abs(rrx) > abs(rry) else 1.0
            color = tuple(int(min(255, c * shade * side)) for c in WALL_COLORS.get(ch, (120, 120, 120)))
            draw.line([(col, max(0, int(top))), (col, min(H - 1, int(bottom)))], fill=color)

            # 竖直接缝：每 30cm 一条，给出透视节奏
            hit_x = self.state.x + rrx * dist
            hit_y = self.state.y + rry * dist
            seam = min(hit_x % CELL, CELL - hit_x % CELL) if abs(rrx) > abs(rry) else \
                min(hit_y % CELL, CELL - hit_y % CELL)
            if seam < 0.02:
                draw.line([(col, max(0, int(top))), (col, min(H - 1, int(bottom)))],
                          fill=tuple(int(c * 0.78) for c in color))
            # 水平砖缝：每 60cm 一道
            k = 1
            while k * 0.6 < WALL_H:
                yy = horizon - (k * 0.6 - CAM_H) * f / dist
                if 0 <= yy < H:
                    draw.line([(col, int(yy)), (col, int(yy) + 1)],
                              fill=tuple(int(c * 0.7) for c in color))
                k += 1

        # ---- 面片（人/杯子/门牌）：按距离从远到近贴
        for sprite in sorted(self.sprites, key=lambda s: -self._sprite_dist(s)):
            self._draw_sprite(draw, sprite, horizon, f, W, H)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=84)
        return buf.getvalue()

    def _sprite_dist(self, sprite: Sprite) -> float:
        return math.hypot(sprite.x - self.state.x, sprite.y - self.state.y)

    def _draw_sprite(self, draw, sprite: Sprite, horizon: int, f: float, W: int, H: int) -> None:
        dx = sprite.x - self.state.x
        dy = sprite.y - self.state.y
        dist = math.hypot(dx, dy)
        if dist < 0.15 or dist > MAX_DIST:
            return
        # 转到相机坐标系
        dir_x, dir_y = math.cos(self.state.theta), math.sin(self.state.theta)
        plane_x, plane_y = -dir_y, dir_x
        inv = 1.0 / (plane_x * dir_y - dir_x * plane_y)
        tx = inv * (dir_y * dx - dir_x * dy)
        ty = inv * (-plane_y * dx + plane_x * dy)
        if ty <= 0.08:
            return
        screen_x = int((W / 2) * (1 + tx / ty))
        w_px = int(sprite.width * f / ty)
        h_px = int(sprite.height * f / ty)
        if w_px < 2 or h_px < 2:
            return
        bottom = int(horizon + CAM_H * f / ty)
        top = bottom - h_px
        left, right = screen_x - w_px // 2, screen_x + w_px // 2
        if right < 0 or left >= W:
            return
        if self._z[min(W - 1, max(0, screen_x))] < ty:   # 被墙挡住
            return
        shade = max(0.40, min(1.0, 1.7 / (1.0 + ty * 0.75)))
        color = tuple(int(min(255, v * shade)) for v in sprite.color)

        if sprite.shape == "person":
            head_r = max(2, w_px // 4)
            head_top = top
            draw.ellipse([screen_x - head_r, head_top, screen_x + head_r, head_top + head_r * 2],
                         fill=tuple(int(v * shade) for v in (222, 184, 152)))
            body_top = head_top + head_r * 2
            draw.rectangle([left, body_top, right, bottom - h_px // 8], fill=color)
            draw.rectangle([screen_x - w_px // 5, bottom - h_px // 8, screen_x + w_px // 5, bottom],
                           fill=tuple(int(v * 0.65) for v in color))
        elif sprite.shape == "sign":
            draw.rectangle([left, top, right, bottom], fill=color)
            if sprite.text and w_px > 40:
                size = max(6, min(12, h_px // 2))
                draw.text((left + max(2, (w_px - len(sprite.text) * size * 0.6) // 2), top + h_px // 5),
                          sprite.text, fill=(28, 28, 32))
        else:
            draw.rectangle([left, top, right, bottom], fill=color)
            draw.rectangle([left, top, right, top + max(1, h_px // 8)],
                           fill=tuple(min(255, int(v * 1.3)) for v in color))

    # ------------------------------------------------------------ 俯视图
    def render_map(self, scale: int = 20) -> bytes:
        """俯视图（文档 / 排查用）：墙、机器人、朝向、视野、面片都在。"""
        from PIL import Image, ImageDraw

        rows, cols = len(MAP), len(MAP[0])
        W, H = cols * scale, rows * scale
        img = Image.new("RGB", (W, H), (24, 24, 27))
        draw = ImageDraw.Draw(img)
        for r in range(rows):
            for c in range(cols):
                ch = MAP[r][c]
                box = [c * scale, r * scale, (c + 1) * scale, (r + 1) * scale]
                if ch == "#":
                    draw.rectangle(box, fill=(62, 68, 80))
                elif ch == "B":
                    draw.rectangle(box, fill=(120, 86, 52))
                else:
                    draw.rectangle(box, fill=(32, 33, 36))
        for sprite in self.sprites:
            sx, sy = sprite.x / CELL * scale, sprite.y / CELL * scale
            rad = max(3, sprite.width / CELL * scale / 2)
            draw.ellipse([sx - rad, sy - rad, sx + rad, sy + rad], fill=sprite.color)
        rx, ry = self.state.x / CELL * scale, self.state.y / CELL * scale
        r = scale * 0.7
        for ang in np.linspace(-FOV / 2, FOV / 2, 11):
            a = self.state.theta + ang
            draw.line([rx, ry, rx + math.cos(a) * r * 8, ry + math.sin(a) * r * 8], fill=(44, 96, 74))
        draw.ellipse([rx - r, ry - r, rx + r, ry + r], fill=(70, 210, 140))
        draw.line([rx, ry, rx + math.cos(self.state.theta) * r * 3, ry + math.sin(self.state.theta) * r * 3],
                  fill=(70, 210, 140), width=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def snapshot(self) -> dict[str, Any]:
        return {**self.state.to_dict(), "sprites": [s.to_dict() for s in self.sprites]}

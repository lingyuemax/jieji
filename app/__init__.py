"""jieji · 解机 —— 看 + 听 + 动，一个能开机器人的大模型智能体。

模块分层：

    capture  →  perception  →  brain  →  safety  →  drivers
    (眼睛耳朵)   (看懂/听懂)    (决策)    (护栏)     (身体)

对外只暴露 server（FastAPI）与 loop（智能体主循环）。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]

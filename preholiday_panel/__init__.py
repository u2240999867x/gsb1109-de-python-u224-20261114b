"""节前客流尖峰对照表构建工具。

基于 workalendar 的门店日历, 用历史流水为每个"节前最后一个营业日"
(观察日) 匹配同星期的对照日, 输出可复算的对照表。
"""
from .panel import (
    PanelConfig,
    PanelResult,
    build_panel,
    diff_panels,
)
from .matching import optimal_assignment, interchangeable_options

__all__ = [
    "PanelConfig",
    "PanelResult",
    "build_panel",
    "diff_panels",
    "optimal_assignment",
    "interchangeable_options",
]

__version__ = "0.1.0"

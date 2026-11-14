"""
Alias module for lead-time (SLA) helpers.

``workalendar.sla`` re-exports the public API of
:mod:`workalendar.leadtime`, so callers may use whichever name fits their
domain best.
"""
from .leadtime import (  # noqa: F401
    Dashboard,
    LeadTime,
    LeadTimeClock,
    LeadTimeDashboard,
    LeadTimeOrder,
    LeadTimeResult,
    LeadTimeState,
    Order,
    Pause,
    PausePeriod,
    analyze_order,
    compute_lead_time,
    dashboard,
    elapsed_working_days,
    lead_time_dashboard,
    order_lead_time,
    summarize_lead_times,
)

__all__ = (
    "LeadTimeState",
    "PausePeriod",
    "LeadTimeOrder",
    "LeadTimeResult",
    "LeadTimeDashboard",
    "LeadTimeClock",
    "elapsed_working_days",
    "compute_lead_time",
    "lead_time_dashboard",
    "summarize_lead_times",
    "Order",
    "Pause",
    "LeadTime",
    "Dashboard",
    "analyze_order",
    "order_lead_time",
    "dashboard",
)

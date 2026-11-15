"""最小费用最大流: 观察日-对照日指派求解器。

匹配目标按字典序:
1. 有对照的观察日尽量多 (流量最大);
2. 在匹配数相同的前提下, 总日期距离最小 (费用最小)。

由于所有边的费用非负, 逐条最短路增广 (SSP) 得到的就是
"最大流中费用最小" 的解, 正好对应上述字典序目标。
"""
from __future__ import annotations

import heapq


class _Edge:
    __slots__ = ("to", "rev", "cap", "cost")

    def __init__(self, to, rev, cap, cost):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost


class MinCostFlow:
    """Successive shortest paths with Johnson potentials."""

    def __init__(self, size):
        self.graph = [[] for _ in range(size)]

    def add_edge(self, fr, to, cap, cost):
        fwd = _Edge(to, len(self.graph[to]), cap, cost)
        rev = _Edge(fr, len(self.graph[fr]), 0, -cost)
        self.graph[fr].append(fwd)
        self.graph[to].append(rev)

    def solve(self, source, sink):
        size = len(self.graph)
        potential = [0] * size
        flow = 0
        cost = 0
        inf = float("inf")
        while True:
            dist = [inf] * size
            dist[source] = 0
            prev_node = [-1] * size
            prev_edge = [-1] * size
            queue = [(0, source)]
            while queue:
                d, node = heapq.heappop(queue)
                if dist[node] < d:
                    continue
                for i, edge in enumerate(self.graph[node]):
                    if edge.cap <= 0:
                        continue
                    nd = d + edge.cost + potential[node] - potential[edge.to]
                    if dist[edge.to] > nd:
                        dist[edge.to] = nd
                        prev_node[edge.to] = node
                        prev_edge[edge.to] = i
                        heapq.heappush(queue, (nd, edge.to))
            if dist[sink] == inf:
                return flow, cost
            for node in range(size):
                if dist[node] < inf:
                    potential[node] += dist[node]
            add = inf
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                add = min(add, edge.cap)
                node = prev_node[node]
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                edge.cap -= add
                self.graph[node][edge.rev].cap += add
                node = prev_node[node]
            flow += add
            # potential[source] 恒为 0, 故 potential[sink] 即本次增广路的真实费用
            cost += add * potential[sink]


def _solve(candidates):
    """candidates: {观察日 key: {对照日期: 距离}}, 返回 (matching, 流量, 费用)。"""
    obs_keys = sorted(candidates)
    all_dates = sorted({d for c in candidates.values() for d in c})
    date_index = {d: j for j, d in enumerate(all_dates)}
    n_obs = len(obs_keys)
    source = 0
    sink = 1 + n_obs + len(all_dates)
    flow = MinCostFlow(sink + 1)
    for i, key in enumerate(obs_keys):
        flow.add_edge(source, 1 + i, 1, 0)
        for d, cost in candidates[key].items():
            flow.add_edge(1 + i, 1 + n_obs + date_index[d], 1, cost)
    for j in range(len(all_dates)):
        flow.add_edge(1 + n_obs + j, sink, 1, 0)
    total_flow, total_cost = flow.solve(source, sink)
    matching = {}
    for i, key in enumerate(obs_keys):
        for edge in flow.graph[1 + i]:
            if edge.to != source and edge.cap == 0:
                matching[key] = all_dates[edge.to - 1 - n_obs]
    return matching, total_flow, total_cost


def optimal_assignment(candidates):
    """求一组最佳指派。

    返回 (matching, n_matched, total_cost); matching 为 {观察日 key: 对照日期}。
    同一对照日期最多被一个观察日使用 (容量为 1)。
    """
    return _solve(candidates)


def interchangeable_options(candidates, n_matched, total_cost):
    """标出每个观察日在所有最佳配法中可互换的对照日期。

    对观察日 o 的候选日期 d: 把 o->d 固定下来 (从子问题中删去 o 与 d),
    若剩余问题仍能达到 (n_matched - 1, total_cost - dist(o, d)),
    则存在一组使用 o->d 的最佳配法, d 即为 o 的可互换对照日。
    """
    options = {}
    for key in sorted(candidates):
        opts = []
        for d, dist in sorted(candidates[key].items()):
            sub = {
                other: {dd: cc for dd, cc in cands.items() if dd != d}
                for other, cands in candidates.items()
                if other != key
            }
            _, sub_flow, sub_cost = _solve(sub)
            if sub_flow == n_matched - 1 and sub_cost == total_cost - dist:
                opts.append(d)
        options[key] = opts
    return options

from collections import defaultdict
from fractions import Fraction
import heapq


def order(ports, links):
    edges = defaultdict(list)
    indegree = dict.fromkeys(ports, 0)
    for link in links:
        edges[link.source].append(link)
        indegree[link.target] += 1
    queue = [port for port in ports if not indegree[port]]
    heapq.heapify(queue)
    nodes = []
    while queue:
        port = heapq.heappop(queue)
        nodes.append(port)
        for link in edges[port]:
            indegree[link.target] -= 1
            if not indegree[link.target]:
                heapq.heappush(queue, link.target)
    if len(nodes) != len(ports):
        raise ValueError('feedback loop in patch sheet')
    return nodes, edges


def matrix(ports, links):
    nodes, outgoing = order(ports, links)
    arrivals = {node: {} for node in nodes}
    for node in nodes:
        if ports[node] == 'feed':
            arrivals[node][node, 0] = (Fraction(1), 1)
        for link in outgoing[node]:
            for (feed, delay), (gain, count) in arrivals[node].items():
                key = (feed, delay + link.delay)
                old_gain, old_count = arrivals[link.target].get(key, (Fraction(0), 0))
                arrivals[link.target][key] = (old_gain + gain * link.gain, old_count + count)
    return arrivals

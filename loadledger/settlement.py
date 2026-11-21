"""Month-end settlement allocation over the Load Ledger warehouse.

A ticket's integer-gram net weight may be split across eligible disposal
contracts.  Allocations are optimized lexicographically:

1. minimize the sum of each contract's shortfall below its minimum grams;
2. among equally good shortfalls, maximize total recognized net grams.

For a reevaluation the third lexicographic goal is to keep as many grams as
possible of the previously signed ``(site, ticket) -> contract`` assignments.

The lexicographic optimization is one min-cost max-flow with edge costs whose
place values do not overlap.  Every capacity is integral, so every produced
allocation uses whole grams.
"""

from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
import json

from .intake import InputError, MATERIALS, identifier, utc_stamp

PRIMARY = 'primary'
UNALLOCATED = ''  # plan slot for per-ticket unallocated remainder output
MAX_SOURCE_BYTES = 16 * 1024 * 1024
# Place-value scale for lexicographic costs. It only has to exceed every
# flow volume the network can carry; a gram total above this is impossible
# for a real input (2e8 g per ticket and bounded ticket count).
COST_SCALE = 10 ** 12


class RosterError(InputError):
    pass


# --------------------------------------------------------------------------- #
# Roster contract
# --------------------------------------------------------------------------- #

def _nonneg_int(value, field):
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise RosterError(f'{field} must be a non-negative integer')
    return value


def _window(pair, contract_id):
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise RosterError(f'contract {contract_id}: window must be [start, end)')
    start, end = utc_stamp(pair[0]), utc_stamp(pair[1])
    if start >= end:
        raise RosterError(f'contract {contract_id}: empty weigh window')
    return start, end


def load_roster(data):
    """Validate a roster payload and return normalized JSON-serializable dict."""
    try:
        text = data.decode('utf-8-sig')
        payload = json.loads(text)
    except UnicodeDecodeError as exc:
        raise RosterError('roster file must be UTF-8') from exc
    except (ValueError, RecursionError) as exc:
        raise RosterError(f'invalid roster JSON: {exc}') from exc
    if not isinstance(payload, dict):
        raise RosterError('roster must be a JSON object')
    roster_id = identifier(payload.get('roster_id'), 'roster_id')
    contracts_in = payload.get('contracts')
    if not isinstance(contracts_in, list) or not contracts_in:
        raise RosterError('roster needs at least one contract')
    contracts, seen = [], set()
    for entry in contracts_in:
        if not isinstance(entry, dict):
            raise RosterError('each contract must be an object')
        contract_id = identifier(entry.get('contract_id'), 'contract_id')
        if contract_id in seen:
            raise RosterError(f'duplicate contract_id: {contract_id}')
        seen.add(contract_id)
        min_g = _nonneg_int(entry.get('min_g'), 'min_g')
        max_g = _nonneg_int(entry.get('max_g'), 'max_g')
        if min_g > max_g:
            raise RosterError(f'contract {contract_id}: min_g exceeds max_g')
        sites = entry.get('sites')
        materials = entry.get('materials')
        if not isinstance(sites, list) or not sites:
            raise RosterError(f'contract {contract_id}: needs allowed sites')
        if not isinstance(materials, list) or not materials:
            raise RosterError(f'contract {contract_id}: needs allowed materials')
        sites = [identifier(s, 'site') for s in sites]
        if len(set(sites)) != len(sites):
            raise RosterError(f'contract {contract_id}: duplicate site')
        for material in materials:
            if not isinstance(material, str) or material not in MATERIALS:
                raise RosterError(f'contract {contract_id}: unknown material')
        if len(set(materials)) != len(materials):
            raise RosterError(f'contract {contract_id}: duplicate material')
        start, end = _window(entry.get('window'), contract_id)
        contracts.append({
            'contract_id': contract_id, 'min_g': min_g, 'max_g': max_g,
            'sites': sorted(sites), 'materials': sorted(materials),
            'window': [start, end],
        })
    return {
        'roster_id': roster_id,
        'contracts': sorted(contracts, key=lambda c: c['contract_id']),
    }


def roster_bytes(roster):
    return (json.dumps(roster, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('utf-8')


# --------------------------------------------------------------------------- #
# Candidate tickets
# --------------------------------------------------------------------------- #

def fetch_candidates(conn, roster):
    """Return currently valid tickets eligible for at least one roster contract."""
    rows = conn.execute('''
        SELECT h.site, h.ticket, h.revision, v.weighed_at, v.material,
               v.gross_g - v.tare_g AS net_g
        FROM ticket_head h JOIN ticket_version v USING(site, ticket, revision)
        WHERE v.operation = 'upsert'
        ORDER BY h.site, h.ticket
    ''').fetchall()
    candidates = []
    for row in rows:
        eligible = []
        for contract in roster['contracts']:
            if (row['site'] in contract['sites']
                    and row['material'] in contract['materials']
                    and contract['window'][0] <= row['weighed_at']
                    < contract['window'][1]):
                eligible.append(contract['contract_id'])
        if eligible:
            candidates.append({
                'site': row['site'], 'ticket': row['ticket'],
                'revision': row['revision'], 'net_g': row['net_g'],
                'weighed_at': row['weighed_at'], 'material': row['material'],
                'eligible_for': eligible,
            })
    return candidates


def candidate_fingerprint(candidates):
    return sha256('\n'.join(
        f"{c['site']}/{c['ticket']}@{c['revision']}:{c['net_g']}"
        for c in candidates).encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------- #
# Min-cost max-flow (successive shortest augmenting path, SPFA, integral)
# --------------------------------------------------------------------------- #

class _Edge:
    __slots__ = ('u', 'v', 'cap', 'cost', 'flow', 'rev', 'tag')

    def __init__(self, u, v, cap, cost, tag):
        self.u, self.v, self.cap, self.cost = u, v, cap, cost
        self.flow, self.rev, self.tag = 0, -1, tag


class MinCostFlow:
    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, cost, tag=None):
        forward = _Edge(u, v, cap, cost, tag)
        backward = _Edge(v, u, 0, -cost, None)
        forward.rev = len(self.g[v])
        backward.rev = len(self.g[u])
        self.g[u].append(forward)
        self.g[v].append(backward)
        return forward

    def solve(self, source, sink):
        """Push flow until no augmenting path remains. All edge costs are
        non-positive in our networks, so reaching max flow automatically
        realizes the lexicographic minimum cost."""
        n, g = self.n, self.g
        total_flow, total_cost = 0, 0
        inf = float('inf')
        while True:
            dist = [inf] * n
            prev_edge = [None] * n
            in_queue = [False] * n
            dist[source] = 0
            queue = deque([source])
            in_queue[source] = True
            while queue:
                u = queue.popleft()
                in_queue[u] = False
                for edge in g[u]:
                    if edge.cap - edge.flow <= 0:
                        continue
                    nd = dist[u] + edge.cost
                    if nd < dist[edge.v]:
                        dist[edge.v] = nd
                        prev_edge[edge.v] = edge
                        if not in_queue[edge.v]:
                            queue.append(edge.v)
                            in_queue[edge.v] = True
            if prev_edge[sink] is None:
                break
            # Always augment to max flow: minfill edges (-COST_SCALE) dominate
            # everything else, so goal ordering stays lexicographic even when a
            # bias gives one contract positive-cost surplus edges.
            bottleneck = inf
            node = sink
            while node != source:
                edge = prev_edge[node]
                bottleneck = min(bottleneck, edge.cap - edge.flow)
                node = edge.u
            node = sink
            while node != source:
                edge = prev_edge[node]
                edge.flow += bottleneck
                g[edge.v][edge.rev].flow -= bottleneck
                node = edge.u
            total_flow += bottleneck
            total_cost += bottleneck * dist[sink]
        return total_flow, total_cost


class _DinicEdge:
    __slots__ = ('src', 'to', 'rev', 'cap', 'tag')

    def __init__(self, src, to, rev, cap, tag):
        self.src, self.to, self.rev, self.cap, self.tag = src, to, rev, cap, tag


class MaxFlow:
    """Plain integral max flow (Dinic) used for parametric range bounds."""

    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, tag=None):
        forward = _DinicEdge(u, v, len(self.g[v]), cap, tag)
        backward = _DinicEdge(v, u, len(self.g[u]), 0, None)
        self.g[u].append(forward)
        self.g[v].append(backward)
        return forward

    def max_flow(self, source, sink):
        n, g = self.n, self.g
        flow = 0
        while True:
            level = [-1] * n
            level[source] = 0
            queue = deque([source])
            while queue:
                u = queue.popleft()
                for edge in g[u]:
                    if edge.cap > 0 and level[edge.to] < 0:
                        level[edge.to] = level[u] + 1
                        queue.append(edge.to)
            if level[sink] < 0:
                return flow
            it = [0] * n

            def dfs(u, pushed):
                if u == sink:
                    return pushed
                while it[u] < len(g[u]):
                    edge = g[u][it[u]]
                    if edge.cap > 0 and level[u] < level[edge.to]:
                        sent = dfs(edge.to, min(pushed, edge.cap))
                        if sent:
                            edge.cap -= sent
                            g[edge.to][edge.rev].cap += sent
                            return sent
                    it[u] += 1
                return 0

            while True:
                pushed = dfs(source, float('inf'))
                if not pushed:
                    break
                flow += pushed
        return flow


# --------------------------------------------------------------------------- #
# Network construction and optimization
# --------------------------------------------------------------------------- #

def _eligibility(candidates, contracts):
    eligible = {c['contract_id']: set() for c in contracts}
    ticket_contracts = []
    for index, cand in enumerate(candidates):
        links = [cid for cid in cand['eligible_for'] if cid in eligible]
        ticket_contracts.append(links)
        for cid in links:
            eligible[cid].add(index)
    return ticket_contracts, eligible


def _build_network(candidates, contracts, ticket_contracts, *, retained=None):
    """Nodes: source, one per ticket, one per contract, sink.

    Ticket -> contract edges carry integer-gram capacity. For reevaluations
    each old assignment gets a parallel ``retained`` edge with cheaper cost so
    the solver preserves old grams first.
    """
    t_count, c_count = len(candidates), len(contracts)
    source = 0
    ticket0 = 1
    contract0 = ticket0 + t_count
    sink = contract0 + c_count
    mcf = MinCostFlow(sink + 1)
    ticket_edges = [[] for _ in range(t_count)]
    retained = retained or {}
    for i, cand in enumerate(candidates):
        mcf.add_edge(source, ticket0 + i, cand['net_g'], 0)
    for j, contract in enumerate(contracts):
        cnode = contract0 + j
        # Min-fill layer first (more negative cost), surplus layer second.
        mcf.add_edge(cnode, sink, contract['min_g'], -COST_SCALE,
                     ('minfill', contract['contract_id']))
        mcf.add_edge(cnode, sink, contract['max_g'] - contract['min_g'], 0,
                     ('surplus', contract['contract_id']))
    for i, links in enumerate(ticket_contracts):
        old_map = retained.get((candidates[i]['site'], candidates[i]['ticket']), {})
        for cid in links:
            j = next(k for k, c in enumerate(contracts)
                     if c['contract_id'] == cid)
            cnode = contract0 + j
            keep = min(old_map.get(cid, 0), candidates[i]['net_g'])
            if keep > 0:
                edge = mcf.add_edge(ticket0 + i, cnode, keep, -1,
                                    ('assign', i, cid, True))
                ticket_edges[i].append(edge)
                extra = candidates[i]['net_g'] - keep
                if extra > 0:
                    edge = mcf.add_edge(ticket0 + i, cnode, extra, 0,
                                        ('assign', i, cid, False))
                    ticket_edges[i].append(edge)
            else:
                edge = mcf.add_edge(ticket0 + i, cnode, candidates[i]['net_g'],
                                    0, ('assign', i, cid, False))
                ticket_edges[i].append(edge)
    return mcf, ticket_edges


def _dinic_plan(dinic, assign_edges, candidates):
    """Recover a plan from a solved Dinic network via residual capacities."""
    plan = {}
    for (i, cid), edge in assign_edges.items():
        # The reverse edge's residual capacity equals the forward flow.
        grams = dinic.g[edge.to][edge.rev].cap
        if grams:
            cand = candidates[i]
            key = (cand['site'], cand['ticket'], cid)
            plan[key] = plan.get(key, 0) + grams
    return plan


def _extract_plan(mcf, ticket_edges, candidates):
    """Return {(site, ticket, contract_id): grams} from a solved network."""
    plan = {}
    for i, edges in enumerate(ticket_edges):
        cand = candidates[i]
        for edge in edges:
            if edge.flow <= 0:
                continue
            _, _, cid, _ = edge.tag
            key = (cand['site'], cand['ticket'], cid)
            plan[key] = plan.get(key, 0) + edge.flow
    return plan


def _plan_contract_totals(plan):
    totals = {}
    for (_, _, cid), grams in plan.items():
        totals[cid] = totals.get(cid, 0) + grams
    return totals


def _range_endpoint(candidates, contracts, ticket_contracts, target_cid, mode):
    """Two-phase capacitated flow giving one contract its range endpoint.

    Phase 1 opens only min-only sink edges, saturating the aggregate goal-1
    minfill flow F1. Phase 2 adds parallel surplus headroom up to each max_g
    on the same residual graph, reaching the goal-2 total F2. A residual
    reroute then pushes the target contract to its extremum while both global
    totals stay fixed.

    Goal 1 is guarded by per-contract flows: a reroute may move grams out of a
    donor only while the receiver's extra ``min(flow, min_g)`` contribution
    covers the donor's lost contribution, so aggregate minfill never drops
    below F1. Every reroute is a closed residual loop, so F2 never changes.
    """
    t_count, c_count = len(candidates), len(contracts)
    source, ticket0 = 0, 1
    contract0 = ticket0 + t_count
    sink = contract0 + c_count
    dinic = MaxFlow(sink + 1)
    assign_edges = {}
    min_of = {}
    for i, cand in enumerate(candidates):
        dinic.add_edge(source, ticket0 + i, cand['net_g'])
    for contract in contracts:
        cid = contract['contract_id']
        min_of[cid] = contract['min_g']
        node = contract0 + _contract_index(contracts, cid)
        dinic.add_edge(node, sink, contract['min_g'],
                       ('minfill', cid))
    for i, links in enumerate(ticket_contracts):
        for cid in links:
            edge = dinic.add_edge(
                ticket0 + i,
                contract0 + _contract_index(contracts, cid),
                candidates[i]['net_g'], ('assign', i, cid))
            assign_edges[(i, cid)] = edge
    f1 = dinic.max_flow(source, sink)
    for contract in contracts:
        node = contract0 + _contract_index(contracts, contract['contract_id'])
        dinic.add_edge(node, sink,
                       contract['max_g'] - contract['min_g'],
                       ('surplus', contract['contract_id']))
    f2 = f1 + dinic.max_flow(source, sink)

    target_node = contract0 + _contract_index(contracts, target_cid)
    _reroute(dinic, target_node, target_cid, sink, into=(mode == 'max'),
             f1=f1, min_of=min_of, contract0=contract0,
             contracts=contracts)
    plan = _dinic_plan(dinic, assign_edges, candidates)
    return f1, f2, plan


def _contract_index(contracts, cid):
    return next(k for k, c in enumerate(contracts)
                if c['contract_id'] == cid)


def _forward_of(dinic, reverse_edge):
    """Return the forward edge corresponding to a residual reverse edge."""
    return dinic.g[reverse_edge.to][reverse_edge.rev]


def _sink_flow(dinic, node, sink):
    """Total grams flowing from a contract node into the sink."""
    return sum(dinic.g[e.to][e.rev].cap for e in dinic.g[node]
               if e.to == sink and e.tag is not None
               and e.tag[0] in ('minfill', 'surplus'))


def _minfill_safe_amount(donor_flow, donor_min, recv_flow, recv_min, limit):
    """Largest x <= limit keeping aggregate minfill at its maximum F1.

    Aggregate minfill is the sum of ``min(flow, min_g)``. Taking x grams from
    the donor reduces its contribution by at most x. The receiver can offset
    that loss with its unmet minfill headroom ``max(0, min_g - flow)``.
    Donor surplus (flow above its minimum) is itself free to move. Hence the
    admissible amount is the sum of both cushions, capped by ``limit``.
    """
    donor_surplus = max(0, donor_flow - donor_min)
    receiver_headroom = max(0, recv_min - recv_flow)
    return min(limit, donor_surplus + receiver_headroom)


def _aggregate_minfill(dinic, cid_to_node, min_of, sink):
    return sum(min(min_of[cid], _sink_flow(dinic, node, sink))
               for cid, node in cid_to_node.items())


def _reroute(dinic, target_node, target_cid, sink, *, into, f1, min_of,
             contract0, contracts):
    """Residual reroute moving recognized grams toward/away from one contract.

    Each iteration BFS-searches a residual loop from a donor contract (reached
    via a reverse sink edge) through alternating contract -> ticket -> contract
    assignment edges to a receiver that still has spare sink capacity. The
    source node is never visited, so every accepted loop leaves total
    source-to-sink flow F2 unchanged.

    Goal 1 is enforced analytically on the two endpoints: moving ``x`` grams
    out of a donor is safe up to its own surplus plus the receiver's unmet
    minfill headroom, which keeps aggregate minfill at its optimum F1. When a
    candidate receiver offers zero safe grams, only that closing sink edge is
    disabled for the iteration; the donor entry stays open because another
    ticket chain may reach a usable receiver.
    """
    g = dinic.g
    cid_to_node = {contract['contract_id']: contract0 + j
                   for j, contract in enumerate(contracts)}
    node_to_cid = {node: cid for cid, node in cid_to_node.items()}
    total_placed = sum(
        _sink_flow(dinic, node, sink) for node in cid_to_node.values())
    # (donor node, receiver node) pairs proven to offer no goal-1-safe grams.
    dead_pairs = set()

    for _ in range(total_placed + len(cid_to_node) + 1):
        path = _find_reroute_path(
            dinic, target_node, target_cid, sink, into=into,
            contract0=contract0, dead_pairs=dead_pairs)
        if path is None:
            return
        bottleneck = min(e.cap for e in path)
        if bottleneck <= 0:
            return
        donor_node = path[0].to
        receiver_node = path[-1].src
        donor_cid = node_to_cid[donor_node]
        recv_cid = node_to_cid[receiver_node]
        safe = _minfill_safe_amount(
            _sink_flow(dinic, donor_node, sink), min_of[donor_cid],
            _sink_flow(dinic, receiver_node, sink), min_of[recv_cid],
            bottleneck)
        if safe <= 0:
            dead_pairs.add((donor_node, receiver_node))
            continue
        for edge in path:
            edge.cap -= safe
            g[edge.to][edge.rev].cap += safe
        # Flows changed; earlier dead-pair verdicts may no longer hold.
        dead_pairs.clear()
        if _aggregate_minfill(dinic, cid_to_node, min_of, sink) < f1:
            raise RuntimeError('reroute violated the minfill invariant')
    raise RuntimeError('reroute did not terminate within the flow bound')


def _find_reroute_path(dinic, target_node, target_cid, sink, *, into,
                       contract0, dead_pairs):
    """Find a residual reroute loop, running one BFS per seed donor.

    A separate BFS per donor keeps the donor identity unambiguous even when
    two donors can reach the same contract node, so a dead (donor, receiver)
    pair never suppresses a different donor's valid path. Node kinds are
    identified structurally and residual assignment edges by their tags.
    """
    g = dinic.g
    ticket_first, ticket_last = 1, contract0 - 1

    def is_ticket(node):
        return ticket_first <= node <= ticket_last

    def is_contract(node):
        return contract0 <= node < sink

    donors = []
    for edge in g[sink]:
        if edge.cap <= 0 or edge.tag is not None:
            continue
        owner = _forward_of(dinic, edge).tag[1]
        if into and owner == target_cid:
            continue
        if not into and owner != target_cid:
            continue
        donors.append(edge.to)

    for donor_node in donors:
        if all((donor_node, receiver) in dead_pairs
               for receiver in range(contract0, sink)):
            continue
        queue = deque([donor_node])
        prev = {donor_node: next(
            e for e in g[sink]
            if e.cap > 0 and e.tag is None and e.to == donor_node)}
        while queue:
            u = queue.popleft()
            for edge in g[u]:
                if edge.cap <= 0 or edge.to in prev:
                    continue
                if edge.to == sink:
                    if edge.tag is None:
                        continue
                    if (donor_node, u) in dead_pairs:
                        continue
                    if (into and u != target_node) or (
                            not into and u == target_node):
                        continue
                    chain = [edge]
                    node = u
                    while node != sink:
                        back = prev[node]
                        chain.append(back)
                        node = back.src
                    chain.reverse()
                    return chain
                if is_ticket(u):
                    if edge.tag is None or edge.tag[0] != 'assign':
                        continue
                    if not is_contract(edge.to):
                        continue
                elif is_contract(u):
                    if edge.tag is not None or not is_ticket(edge.to):
                        continue
                else:
                    continue
                prev[edge.to] = edge
                queue.append(edge.to)
    return None


def optimize(candidates, contracts, *, retained=None):
    """Produce the primary optimal plan plus per-contract recognized ranges.

    The optimum is lexicographic: (1) minimize total shortfall below contract
    minima, then (2) maximize recognized grams. Each contract's range spans
    exactly the grams it can receive across the allocations attaining both
    goals; both endpoint plans are returned for review.

    All capacities are integral, hence every plan uses whole grams and never
    exceeds a ticket's net weight. For reevaluations, cheap retained edges in
    the primary network make preserving old (site, ticket) -> contract grams
    the third lexicographic goal.
    """
    ticket_contracts, _ = _eligibility(candidates, contracts)
    min_total = sum(c['min_g'] for c in contracts)

    # Primary lexicographic plan (with retention edges when reevaluating).
    mcf, ticket_edges = _build_network(
        candidates, contracts, ticket_contracts, retained=retained)
    source = 0
    sink = 1 + len(candidates) + len(contracts)
    f2, _ = mcf.solve(source, sink)
    primary = _extract_plan(mcf, ticket_edges, candidates)
    f1 = sum(
        edge.flow for node_edges in mcf.g for edge in node_edges
        if edge.tag is not None and edge.tag[0] == 'minfill')
    min_shortfall = min_total - f1

    ranges = {}
    min_of = {c['contract_id']: c['min_g'] for c in contracts}
    for j, contract in enumerate(contracts):
        cid = contract['contract_id']
        min_f1, min_f2, min_plan = _range_endpoint(
            candidates, contracts, ticket_contracts, cid, 'min')
        max_f1, max_f2, max_plan = _range_endpoint(
            candidates, contracts, ticket_contracts, cid, 'max')
        # Both endpoint plans attain the two global goals: full recognized
        # total F2 and the minimum aggregate shortfall.
        def shortfall(plan):
            totals = _plan_contract_totals(plan)
            return sum(max(0, min_of[c] - totals.get(c, 0))
                       for c in min_of)
        assert shortfall(min_plan) == min_shortfall
        assert shortfall(max_plan) == min_shortfall
        assert min_f2 == f2 and max_f2 == f2
        cmin = _plan_contract_totals(min_plan).get(cid, 0)
        cmax = _plan_contract_totals(max_plan).get(cid, 0)
        assert cmin <= cmax
        ranges[cid] = {
            'min_g': cmin,
            'max_g': cmax,
            'min_plan': min_plan,
            'max_plan': max_plan,
        }
    return {
        'min_shortfall_g': min_shortfall,
        'max_recognized_g': f2,
        'primary': primary,
        'ranges': ranges,
    }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace(
        '+00:00', 'Z')


def _insert_doc(conn, doc_id, roster_id, kind, parent_doc_id, result,
                candidates, status, fingerprint, selected_plan, retained_g):
    conn.execute('''
        INSERT INTO settlement_doc
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (doc_id, roster_id, kind, parent_doc_id, _now(), status, None,
          selected_plan, result['min_shortfall_g'], result['max_recognized_g'],
          retained_g, fingerprint))
    for cand in candidates:
        conn.execute('INSERT INTO doc_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                     (doc_id, cand['site'], cand['ticket'], cand['revision'],
                      cand['net_g'], ','.join(cand['eligible_for']),
                      1 if cand.get('still_valid', True) else 0,
                      cand.get('invalid_reason')))
    plans = {PRIMARY: result['primary']}
    for cid, info in result['ranges'].items():
        plans[f'{cid}:min'] = info['min_plan']
        plans[f'{cid}:max'] = info['max_plan']
    for plan_name, plan in sorted(plans.items()):
        for (site, ticket, cid), grams in sorted(plan.items()):
            retained = 0
            if plan_name == PRIMARY:
                old_map = (result.get('retained_map') or {}).get((site, ticket), {})
                retained = min(old_map.get(cid, 0), grams)
            conn.execute('INSERT INTO doc_allocation VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (doc_id, plan_name, site, ticket, cid, grams, retained))


def create_roster(conn, data):
    roster = load_roster(data)
    roster_id = roster['roster_id']
    existing = conn.execute(
        'SELECT roster_json FROM settlement_roster WHERE roster_id = ?',
        (roster_id,)).fetchone()
    payload = roster_bytes(roster)
    if existing is not None:
        if existing['roster_json'] != payload.decode('utf-8'):
            raise RosterError(f'roster_id {roster_id} already frozen differently')
        return {'roster_id': roster_id, 'status': 'replay'}
    conn.execute('INSERT INTO settlement_roster VALUES (?, ?, ?, ?)',
                 (roster_id, _now(), payload.decode('utf-8'),
                  sha256(payload).hexdigest()))
    for contract in roster['contracts']:
        conn.execute('INSERT INTO settlement_contract VALUES (?, ?, ?, ?, ?, ?)',
                     (roster_id, contract['contract_id'], contract['min_g'],
                      contract['max_g'], contract['window'][0],
                      contract['window'][1]))
        for site in contract['sites']:
            conn.execute('INSERT INTO contract_allowed_site VALUES (?, ?, ?)',
                         (roster_id, contract['contract_id'], site))
        for material in contract['materials']:
            conn.execute('INSERT INTO contract_allowed_material VALUES (?, ?, ?)',
                         (roster_id, contract['contract_id'], material))
    return {'roster_id': roster_id, 'status': 'frozen',
            'contracts': len(roster['contracts'])}


def _load_frozen_roster(conn, roster_id):
    row = conn.execute(
        'SELECT roster_json FROM settlement_roster WHERE roster_id = ?',
        (roster_id,)).fetchone()
    if row is None:
        raise RosterError(f'unknown roster: {roster_id}')
    return json.loads(row['roster_json'])


def propose(conn, doc_id, roster_id, *, selected_plan=PRIMARY):
    identifier(doc_id, 'doc_id')
    roster = _load_frozen_roster(conn, roster_id)
    if conn.execute('SELECT 1 FROM settlement_doc WHERE doc_id = ?',
                    (doc_id,)).fetchone():
        raise RosterError(f'doc_id already exists: {doc_id}')
    candidates = fetch_candidates(conn, roster)
    result = optimize(candidates, roster['contracts'])
    _validate_selected_plan(selected_plan, result)
    fingerprint = candidate_fingerprint(candidates)
    _insert_doc(conn, doc_id, roster_id, 'proposal', None, result, candidates,
                'open', fingerprint, selected_plan, None)
    return show(conn, doc_id)


def _validate_selected_plan(selected_plan, result):
    valid = {PRIMARY}
    for cid in result['ranges']:
        valid.update((f'{cid}:min', f'{cid}:max'))
    if selected_plan not in valid:
        raise RosterError(f'unknown plan: {selected_plan}')


def _stale_candidates(conn, doc):
    """Re-check frozen candidates against current warehouse state.

    Returns (is_stale, candidates_with_validity). A proposal is stale when any
    frozen ticket changed revision, became void, or stopped matching its
    eligibility -- even if site/material net-weight totals happen to be
    unchanged. Newly arrived eligible tickets also change the candidate set.
    """
    roster = _load_frozen_roster(conn, doc['roster_id'])
    frozen = conn.execute(
        'SELECT * FROM doc_candidate WHERE doc_id = ? ORDER BY site, ticket',
        (doc['doc_id'],)).fetchall()
    current = {(c['site'], c['ticket']): c
               for c in fetch_candidates(conn, roster)}
    stale = False
    enriched = []
    for row in frozen:
        key = (row['site'], row['ticket'])
        cand = dict(row)
        cand['eligible_for'] = row['eligible_for'].split(',')
        head = conn.execute('''
            SELECT h.revision, v.operation, v.weighed_at, v.material,
                   v.gross_g - v.tare_g AS net_g
            FROM ticket_head h JOIN ticket_version v USING(site, ticket, revision)
            WHERE h.site = ? AND h.ticket = ?
        ''', key).fetchone()
        reason = None
        if head is None:
            reason = 'ticket-removed'
        elif head['operation'] == 'void':
            reason = 'ticket-voided'
        elif head['revision'] != row['revision']:
            reason = 'revision-superseded'
        elif key not in current:
            reason = 'no-longer-eligible'
        if reason is not None:
            stale = True
            cand['still_valid'] = False
            cand['invalid_reason'] = reason
        else:
            cand.update({'weighed_at': head['weighed_at'],
                         'material': head['material'],
                         'still_valid': True, 'invalid_reason': None})
        enriched.append(cand)
    if len(current) != len(frozen) or any(
            (c['site'], c['ticket']) not in
            {(r['site'], r['ticket']) for r in frozen}
            for c in current.values()):
        stale = True
    return stale, enriched, current


def sign(conn, doc_id, *, selected_plan=None):
    doc = conn.execute('SELECT * FROM settlement_doc WHERE doc_id = ?',
                       (doc_id,)).fetchone()
    if doc is None:
        raise RosterError(f'unknown doc: {doc_id}')
    if doc['status'] != 'open':
        raise RosterError(f'doc {doc_id} is not open (status={doc["status"]})')
    if selected_plan is None:
        selected_plan = doc['selected_plan']
    result = _load_result(conn, doc_id)
    _validate_selected_plan(selected_plan, result)
    if doc['kind'] == 'proposal':
        # A proposal freezes one warehouse snapshot; it is signable only while
        # that snapshot is still current. Reevaluations are themselves built
        # from the latest snapshot, so they need no further freshness gate.
        stale, _, _ = _stale_candidates(conn, doc)
        if stale:
            raise RosterError(
                f'proposal {doc_id} expired: candidate revisions changed since '
                'it was frozen; mark it expired and issue a reevaluation instead')
    conn.execute('''
        UPDATE settlement_doc
        SET status = 'signed', signed_at = ?, selected_plan = ?
        WHERE doc_id = ?
    ''', (_now(), selected_plan, doc_id))
    return show(conn, doc_id)


def mark_expired(conn, doc_id):
    doc = conn.execute('SELECT * FROM settlement_doc WHERE doc_id = ?',
                       (doc_id,)).fetchone()
    if doc is None:
        raise RosterError(f'unknown doc: {doc_id}')
    if doc['kind'] != 'proposal' or doc['status'] != 'open':
        raise RosterError(f'doc {doc_id} is not an open proposal')
    stale, _, _ = _stale_candidates(conn, doc)
    if not stale:
        raise RosterError(f'proposal {doc_id} still matches the warehouse and '
                          'can be signed')
    conn.execute(
        "UPDATE settlement_doc SET status = 'expired' WHERE doc_id = ?",
        (doc_id,))
    return show(conn, doc_id)


# --------------------------------------------------------------------------- #
# Reevaluation after correction or void
# --------------------------------------------------------------------------- #

def _signed_plan(conn, doc_id):
    doc = conn.execute('SELECT * FROM settlement_doc WHERE doc_id = ?',
                       (doc_id,)).fetchone()
    if doc is None:
        raise RosterError(f'unknown doc: {doc_id}')
    if doc['status'] != 'signed':
        raise RosterError(f'doc {doc_id} is not signed')
    rows = conn.execute('''
        SELECT site, ticket, contract_id, grams FROM doc_allocation
        WHERE doc_id = ? AND plan_name = ?
    ''', (doc_id, doc['selected_plan'])).fetchall()
    plan = {}
    for row in rows:
        plan.setdefault((row['site'], row['ticket']), {})[
            row['contract_id']] = row['grams']
    return doc, plan


def reevaluate(conn, new_doc_id, parent_doc_id, *, selected_plan=PRIMARY):
    identifier(new_doc_id, 'doc_id')
    if conn.execute('SELECT 1 FROM settlement_doc WHERE doc_id = ?',
                    (new_doc_id,)).fetchone():
        raise RosterError(f'doc_id already exists: {new_doc_id}')
    parent, old_plan = _signed_plan(conn, parent_doc_id)
    roster = _load_frozen_roster(conn, parent['roster_id'])
    candidates = fetch_candidates(conn, roster)
    current_keys = {(c['site'], c['ticket']) for c in candidates}
    # Annotate frozen-ticket validity so fully un-retentive lines can be listed.
    for cand in candidates:
        cand['still_valid'] = True
        cand['invalid_reason'] = None
    invalid = []
    for (site, ticket), alloc in old_plan.items():
        if (site, ticket) in current_keys:
            continue
        head = conn.execute('''
            SELECT v.operation FROM ticket_head h
            JOIN ticket_version v USING(site, ticket, revision)
            WHERE h.site = ? AND h.ticket = ?
        ''', (site, ticket)).fetchone()
        if head is None:
            reason = 'ticket-removed'
        elif head['operation'] == 'void':
            reason = 'ticket-voided'
        else:
            reason = 'no-longer-eligible'
        invalid.append({'site': site, 'ticket': ticket,
                        'invalid_reason': reason, 'allocations': dict(alloc)})
    result = optimize(candidates, roster['contracts'], retained=old_plan)
    _validate_selected_plan(selected_plan, result)
    retained_map = {}
    retained_total = 0
    for (site, ticket, cid), grams in result['primary'].items():
        old = old_plan.get((site, ticket), {}).get(cid, 0)
        kept = min(old, grams)
        if kept:
            retained_map.setdefault((site, ticket), {})[cid] = kept
            retained_total += kept
    result['retained_map'] = retained_map
    # Range plans are reviewable alternatives under the same goals; they do not
    # themselves carry retention tags.
    fingerprint = candidate_fingerprint(candidates)
    _insert_doc(conn, new_doc_id, parent['roster_id'], 'reevaluation',
                parent_doc_id, result, candidates, 'open', fingerprint,
                selected_plan, retained_total)
    for item in invalid:
        conn.execute('''
            INSERT INTO doc_invalid_candidate
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (new_doc_id, item['site'], item['ticket'],
              ','.join(sorted(item['allocations'])),
              item['invalid_reason'],
              json.dumps(item['allocations'], sort_keys=True)))
    return show(conn, new_doc_id)


# --------------------------------------------------------------------------- #
# Read models
# --------------------------------------------------------------------------- #

def _load_result(conn, doc_id):
    doc = conn.execute('SELECT * FROM settlement_doc WHERE doc_id = ?',
                       (doc_id,)).fetchone()
    if doc is None:
        raise RosterError(f'unknown doc: {doc_id}')
    plans = {}
    rows = conn.execute('''
        SELECT plan_name, site, ticket, contract_id, grams, retained_g
        FROM doc_allocation WHERE doc_id = ?
        ORDER BY plan_name, site, ticket, contract_id
    ''', (doc_id,)).fetchall()
    retained_map = {}
    for row in rows:
        plan = plans.setdefault(row['plan_name'], {})
        plan[(row['site'], row['ticket'], row['contract_id'])] = row['grams']
        if row['retained_g']:
            retained_map.setdefault((row['site'], row['ticket']), {})[
                row['contract_id']] = row['retained_g']
    primary = plans.pop(PRIMARY, {})
    ranges = {}
    for plan_name, plan in list(plans.items()):
        cid, endpoint = plan_name.split(':')
        info = ranges.setdefault(cid, {})
        info[{'min': 'min_plan', 'max': 'max_plan'}[endpoint]] = plan
    for cid, info in ranges.items():
        info['min_g'] = _plan_contract_totals(info['min_plan']).get(cid, 0)
        info['max_g'] = _plan_contract_totals(info['max_plan']).get(cid, 0)
    return {
        'doc': doc,
        'min_shortfall_g': doc['min_shortfall_g'],
        'max_recognized_g': doc['max_recognized_g'],
        'primary': primary,
        'ranges': ranges,
        'retained_map': retained_map,
    }


def _plan_lines(plan):
    return [
        {'site': site, 'ticket': ticket, 'contract_id': cid, 'grams': grams}
        for (site, ticket, cid), grams in sorted(plan.items())
    ]


def _candidate_view(conn, doc_id):
    rows = conn.execute('''
        SELECT doc_id, site, ticket, revision, net_g, eligible_for,
               still_valid, invalid_reason
        FROM doc_candidate WHERE doc_id = ?
        UNION ALL
        SELECT doc_id, site, ticket, NULL AS revision, 0 AS net_g,
               eligible_for, 0 AS still_valid, invalid_reason
        FROM doc_invalid_candidate WHERE doc_id = ?
        ORDER BY site, ticket
    ''', (doc_id, doc_id)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item['eligible_for'] = row['eligible_for'].split(',')
        out.append(item)
    return out


def show(conn, doc_id):
    result = _load_result(conn, doc_id)
    doc = result['doc']
    candidates = _candidate_view(conn, doc_id)
    valid = [c for c in candidates if c['still_valid']]
    primary_lines = _plan_lines(result['primary'])
    allocated = {}
    for line in primary_lines:
        key = (line['site'], line['ticket'])
        allocated[key] = allocated.get(key, 0) + line['grams']
    unallocated = []
    for cand in valid:
        used = allocated.get((cand['site'], cand['ticket']), 0)
        unallocated.append({
            'site': cand['site'], 'ticket': cand['ticket'],
            'net_g': cand['net_g'], 'allocated_g': used,
            'unallocated_g': cand['net_g'] - used,
        })
    totals = _plan_contract_totals(result['primary'])
    roster = _load_frozen_roster(conn, doc['roster_id'])
    contracts_out = []
    for contract in roster['contracts']:
        cid = contract['contract_id']
        recognized = totals.get(cid, 0)
        info = result['ranges'].get(cid, {'min_g': recognized,
                                          'max_g': recognized})
        endpoints = []
        if cid in result['ranges']:
            endpoints = [
                {'endpoint': 'min', 'grams': result['ranges'][cid]['min_g'],
                 'plan_name': f'{cid}:min',
                 'allocations': _plan_lines(result['ranges'][cid]['min_plan'])},
                {'endpoint': 'max', 'grams': result['ranges'][cid]['max_g'],
                 'plan_name': f'{cid}:max',
                 'allocations': _plan_lines(result['ranges'][cid]['max_plan'])},
            ]
        contracts_out.append({
            'contract_id': cid,
            'min_g': contract['min_g'], 'max_g': contract['max_g'],
            'recognized_g': recognized,
            'shortfall_g': max(0, contract['min_g'] - recognized),
            'shortfall_in_every_optimum':
                result['ranges'][cid]['max_g'] < contract['min_g']
                if cid in result['ranges'] else recognized < contract['min_g'],
            'range_g': {
                'min': result['ranges'][cid]['min_g']
                if cid in result['ranges'] else recognized,
                'max': result['ranges'][cid]['max_g']
                if cid in result['ranges'] else recognized,
            },
            'endpoint_plans': endpoints,
        })
    payload = {
        'doc_id': doc_id,
        'kind': doc['kind'],
        'status': doc['status'],
        'roster_id': doc['roster_id'],
        'parent_doc_id': doc['parent_doc_id'],
        'created_at': doc['created_at'],
        'signed_at': doc['signed_at'],
        'selected_plan': doc['selected_plan'],
        'candidate_fingerprint': doc['candidate_fingerprint'],
        'objectives': {
            'min_shortfall_g': result['min_shortfall_g'],
            'max_recognized_g': result['max_recognized_g'],
        },
        'candidates': candidates,
        'contracts': contracts_out,
        'plans': {'primary': primary_lines},
        'unallocated': unallocated,
    }
    if doc['kind'] == 'reevaluation':
        payload['retained_g'] = doc['retained_g']
        payload['diff'] = build_diff(conn, doc_id)
    return payload


def build_diff(conn, doc_id):
    """Old vs new signed/selected allocation details."""
    doc = conn.execute('SELECT * FROM settlement_doc WHERE doc_id = ?',
                       (doc_id,)).fetchone()
    if doc is None or doc['parent_doc_id'] is None:
        raise RosterError(f'doc {doc_id} has no parent to diff against')
    result = _load_result(conn, doc_id)
    _, old_plan = _signed_plan(conn, doc['parent_doc_id'])
    current = _candidate_view(conn, doc_id)
    valid_keys = {(c['site'], c['ticket']) for c in current if c['still_valid']}
    invalid_reason = {
        (c['site'], c['ticket']): c['invalid_reason']
        for c in current if not c['still_valid']}
    new_flat = {}
    for (site, ticket, cid), grams in result['primary'].items():
        new_flat[(site, ticket, cid)] = grams

    fully_lost, reduced, added = [], [], []
    old_total = new_total = retained_total = 0
    contract_old, contract_new = {}, {}
    for (site, ticket), alloc in old_plan.items():
        for cid, grams in alloc.items():
            old_total += grams
            contract_old[cid] = contract_old.get(cid, 0) + grams
    for (site, ticket, cid), grams in new_flat.items():
        new_total += grams
        contract_new[cid] = contract_new.get(cid, 0) + grams

    for (site, ticket), alloc in sorted(old_plan.items()):
        for cid, old_g in sorted(alloc.items()):
            key = (site, ticket)
            if key not in valid_keys:
                fully_lost.append({
                    'site': site, 'ticket': ticket, 'contract_id': cid,
                    'old_g': old_g, 'new_g': 0,
                    'reason': invalid_reason.get(key, 'ticket-invalid'),
                })
                continue
            new_g = new_flat.get((site, ticket, cid), 0)
            kept = min(old_g, new_g)
            retained_total += kept
            if new_g < old_g:
                reduced.append({
                    'site': site, 'ticket': ticket, 'contract_id': cid,
                    'old_g': old_g, 'new_g': new_g,
                    'retained_g': kept, 'removed_g': old_g - kept,
                })
    for (site, ticket, cid), new_g in sorted(new_flat.items()):
        old_g = old_plan.get((site, ticket), {}).get(cid, 0)
        if new_g > old_g:
            added.append({
                'site': site, 'ticket': ticket, 'contract_id': cid,
                'old_g': old_g, 'new_g': new_g, 'added_g': new_g - old_g,
            })
    roster = _load_frozen_roster(conn, doc['roster_id'])
    contract_changes = []
    for contract in roster['contracts']:
        cid = contract['contract_id']
        old_g = contract_old.get(cid, 0)
        new_g = contract_new.get(cid, 0)
        contract_changes.append({
            'contract_id': cid, 'old_g': old_g, 'new_g': new_g,
            'delta_g': new_g - old_g,
            'new_shortfall_g': max(0, contract['min_g'] - new_g),
        })
    return {
        'parent_doc_id': doc['parent_doc_id'],
        'old_total_g': old_total,
        'new_total_g': new_total,
        'retained_g': retained_total,
        'fully_unretentive': fully_lost,
        'reduced': reduced,
        'added': added,
        'contract_changes': sorted(contract_changes,
                                   key=lambda c: c['contract_id']),
    }


def doc_lineage(conn, doc_id):
    """Trace every gram of every plan back to a valid revision and batch rows."""
    doc = conn.execute('SELECT * FROM settlement_doc WHERE doc_id = ?',
                       (doc_id,)).fetchone()
    if doc is None:
        raise RosterError(f'unknown doc: {doc_id}')
    candidates = {
        (c['site'], c['ticket']): c for c in _candidate_view(conn, doc_id)}
    lines = conn.execute('''
        SELECT d.plan_name, d.site, d.ticket, d.contract_id, d.grams,
               d.retained_g, c.revision
        FROM doc_allocation d JOIN doc_candidate c
          ON d.doc_id = c.doc_id AND d.site = c.site AND d.ticket = c.ticket
        WHERE d.doc_id = ?
        ORDER BY d.plan_name, d.site, d.ticket, d.contract_id
    ''', (doc_id,)).fetchall()
    allocations = []
    for row in lines:
        sources = conn.execute('''
            SELECT r.batch_id, r.line, b.source_id, b.format_name, b.sha256
            FROM source_row r JOIN source_batch b USING(batch_id)
            WHERE r.site = ? AND r.ticket = ? AND r.revision = ?
            ORDER BY r.batch_id, r.line
        ''', (row['site'], row['ticket'], row['revision'])).fetchall()
        allocations.append({
            'plan_name': row['plan_name'], 'site': row['site'],
            'ticket': row['ticket'], 'revision': row['revision'],
            'contract_id': row['contract_id'], 'grams': row['grams'],
            'retained_g': row['retained_g'],
            'sources': [dict(s) for s in sources],
        })
    chain = []
    current = doc
    while current is not None:
        chain.append({'doc_id': current['doc_id'], 'kind': current['kind'],
                      'status': current['status'],
                      'parent_doc_id': current['parent_doc_id']})
        if current['parent_doc_id'] is None:
            break
        current = conn.execute(
            'SELECT * FROM settlement_doc WHERE doc_id = ?',
            (current['parent_doc_id'],)).fetchone()
    return {
        'doc_id': doc_id,
        'roster_id': doc['roster_id'],
        'doc_chain': chain,
        'candidate_revisions': [
            {'site': c['site'], 'ticket': c['ticket'],
             'revision': c['revision'], 'net_g': c['net_g'],
             'still_valid': bool(c['still_valid']),
             'invalid_reason': c['invalid_reason']}
            for c in candidates.values()],
        'allocations': allocations,
    }

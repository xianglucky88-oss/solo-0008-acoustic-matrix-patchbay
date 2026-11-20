from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Link:
    name: str
    source: str
    target: str
    delay: int
    gain: Fraction


def parse(document):
    ports = {}
    for row in document['ports']:
        name, role = row['id'], row['role']
        if not isinstance(name, str) or not name or name in ports or role not in ('feed', 'bus', 'tap'):
            raise ValueError(f'invalid port: {name}')
        ports[name] = role
    if not 1 <= len(ports) <= 128 or not {'feed', 'tap'} <= set(ports.values()):
        raise ValueError('a patch needs at most 128 ports and at least one feed and tap')
    links, names = [], set()
    for row in document['links']:
        name, source, target, delay = (row[field] for field in ('id', 'from', 'to', 'delay'))
        numerator, denominator = row['gain']
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f'invalid link id: {name}')
        if ports.get(source) not in ('feed', 'bus') or ports.get(target) not in ('bus', 'tap'):
            raise ValueError(f'invalid port direction in {name}')
        if (type(delay) is not int or not 0 <= delay <= 64 or type(numerator) is not int
            or not 1 <= abs(numerator) <= 16 or type(denominator) is not int or not 1 <= denominator <= 16):
            raise ValueError(f'invalid gain or delay in {name}')
        links.append(Link(name, source, target, delay, Fraction(numerator, denominator)))
        names.add(name)
    if len(links) > 256:
        raise ValueError('at most 256 patch links')
    return ports, links

from .influence import matrix
from .intake import parse


def run(document):
    ports, links = parse(document)
    arrivals = matrix(ports, links)
    return {'taps': [
        {'tap': node, 'arrivals': [
            {'feed': feed, 'delay': delay, 'gain': [gain.numerator, gain.denominator], 'path_count': count}
            for (feed, delay), (gain, count) in sorted(arrivals[node].items())]}
        for node in sorted(ports) if ports[node] == 'tap']}

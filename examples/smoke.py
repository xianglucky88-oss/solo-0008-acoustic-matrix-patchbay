import json
from pathlib import Path
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
doc = json.loads((HERE / 'demo.json').read_text(encoding='utf-8'))


def invoke(payload, expected_error=None):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'source.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        cmd = [sys.executable, '-B', '-m', 'patchmatrix', str(path)]
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=15)
    if expected_error:
        assert result.returncode == 2 and not result.stdout and expected_error in result.stderr, result
    else:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


out = invoke(doc)
record, spare = out['taps']
assert spare['arrivals'] == []
assert record['arrivals'] == [{'feed':'desk', 'delay':3, 'gain':[0,1], 'path_count':2}, {'feed':'desk', 'delay':4, 'gain':[1,3], 'path_count':1}]
doc['links'].extend([{'id':'X1','from':'left-sum','to':'right-sum','delay':0,'gain':[1,1]}, {'id':'X2','from':'right-sum','to':'left-sum','delay':0,'gain':[1,1]}])
invoke(doc, 'feedback loop')
print('production smoke: PASS (patchmatrix)')

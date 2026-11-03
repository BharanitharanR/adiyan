"""
Camera-friendly live view of mesh/lib/llm_log.py's central log, for the
Wayfinder demo recording - the raw log file is full of long prompt/
response text, unreadable on screen; this prints one compact line per
call as it happens: time, agent.stage, model, token count. Nothing else.

Run from the repo root:
    .venv/bin/python3 -m mesh.tools.demo_log_tailer
"""
import re
import time
from pathlib import Path

LOG_PATH = Path.home() / '.Adiyan' / 'logs' / 'llm_calls.log'
HEADER_RE = re.compile(r'^=== (\S+) \| agent=(\S+) \| stage=(\S+) \| mode=(\S+) \| model=(\S+) ===$')
USAGE_RE = re.compile(r'^input_tokens=(\d+) output_tokens=(\d+) total_tokens=(\d+)$')


def main() -> None:
    print('Watching for Meta Model API calls... (Ctrl+C to stop)\n')
    pending = None
    with open(LOG_PATH, 'r') as f:
        f.seek(0, 2)  # start at end - only new calls from now on
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            line = line.rstrip('\n')
            header = HEADER_RE.match(line)
            if header:
                _, agent, stage, mode, model = header.groups()
                pending = (agent, stage, model)
                continue
            usage = USAGE_RE.match(line)
            if usage and pending:
                agent, stage, model = pending
                _, _, total = usage.groups()
                ts = time.strftime('%H:%M:%S')
                print(f'{ts}  {agent}.{stage:<28}  {model:<28}  {total:>6} tokens')
                pending = None


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Read a TGOFFLINE install report and answer small queries about it.

The report file is marker-delimited JSON (``TGOFFLINE-REPORT-BEGIN`` … ``TGOFFLINE-REPORT-END``)
so it can be streamed; the driver's report nests the router-side report under
``remote_report``.

usage:
  report.py <file> get <key>            print one field (top level)
  report.py <file> remote-get <key>     print one field of the embedded router report
  report.py <file> fails                count failing gate_* entries (top level)
  report.py <file> remote-fails         count failing gate_* entries in the embedded report
  report.py <file> missing <key>...     print the required keys that are absent
  report.py <file> remote-key-count     print the number of keys in the embedded report
"""
import json
import sys


def load(path):
    raw = open(path, encoding="utf-8").read()
    begin, end = "TGOFFLINE-REPORT-BEGIN", "TGOFFLINE-REPORT-END"
    if begin in raw:
        block = raw[raw.index(begin) + len(begin):raw.index(end)]
        return json.loads(block)
    return json.loads(raw)


def main(argv):
    path, action = argv[1], argv[2]
    data = load(path)
    if action == "get":
        print(data.get(argv[3], ""))
    elif action == "remote-get":
        print((data.get("remote_report") or {}).get(argv[3], ""))
    elif action == "fails":
        print(sum(1 for k, v in data.items() if k.startswith("gate_") and v == "fail"))
    elif action == "remote-fails":
        remote = data.get("remote_report") or {}
        print(sum(1 for k, v in remote.items() if k.startswith("gate_") and v == "fail"))
    elif action == "missing":
        need = argv[3:]
        print(",".join(k for k in need if k not in data))
    elif action == "remote-key-count":
        remote = data.get("remote_report")
        print(len(remote) if isinstance(remote, dict) else -1)
    else:
        raise SystemExit(f"unknown action {action!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

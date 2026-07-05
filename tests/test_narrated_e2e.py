"""End-to-end driver for the Narrated Recap flow — runs each step via the API
and prints stage/error after each step. Usage:
    ./venv/bin/python test_narrated_e2e.py <project_name> [source_folder] [steps...]
"""
import json
import sys
import time
import urllib.request

BASE = "http://localhost:8080"


def api(path, data=None, method=None):
    url = BASE + path
    if data is not None:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method=method or "POST")
    else:
        req = urllib.request.Request(url, method=method or "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": f"HTTP {e.code}"}


def wait_done(timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = api("/api/debug")
        if not d.get("thread_alive"):
            return True
        time.sleep(2)
    return False


def get_state(name):
    return api(f"/api/projects/{name}")


def run_step(step, project, extra=None):
    payload = {"project_name": project}
    if extra:
        payload.update(extra)
    r = api(f"/api/run/step/{step}", payload)
    if r.get("status") != "started":
        print(f"  !! {step} did not start: {r}")
        return False
    ok = wait_done()
    st = get_state(project)
    print(f"  [{step}] stage={st.get('stage')} error={st.get('error')} "
          f"panels={len(st.get('panels') or [])} pwt={len(st.get('panels_with_text') or [])}")
    return ok and not st.get("error")


def main():
    project = sys.argv[1]
    source = sys.argv[2] if len(sys.argv) > 2 else ""
    steps = sys.argv[3:] or ["load", "stitch", "detect1", "crop1", "stitch2",
                             "detect2", "crop2", "extract_dialogue"]
    if source:
        if not run_step("load", project, {"source_folder": source}):
            sys.exit(1)
    for step in steps:
        if step == "load":
            continue
        print(f"▶ {step}")
        if not run_step(step, project):
            print(f"STOP at {step}")
            sys.exit(1)
    print("DONE")


if __name__ == "__main__":
    main()

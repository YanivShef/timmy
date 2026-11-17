# usage: run_case.py <name> <case-stem> <instructions-json> [claude_path]
import sys, json, os, shutil, subprocess, time
Q = "/Users/yaniv/Projects/timmy/qa-improve"
name, stem, instr = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
claude = sys.argv[4] if len(sys.argv) > 4 else os.path.expanduser("~/.nvm/versions/node/v22.12.0/bin/claude")
app = f"{Q}/runs/{name}/appdir"; out = f"{Q}/runs/{name}/out"
os.makedirs(app, exist_ok=True); os.makedirs(out, exist_ok=True)
shutil.copy(f"{Q}/appdir/live.py", app)
if not os.path.exists(app + "/venv"): os.symlink(os.path.expanduser("~/.venvs/mlx-whisper"), app + "/venv")
json.dump({"owner_name": "יניב", "claude_path": claude, "output_dir": out, "custom_instructions": instr}, open(app + "/config.json", "w"), ensure_ascii=False)
for suf in (".md", " - תמלול.txt"):
    if os.path.exists(f"{Q}/cases/{stem}{suf}"): shutil.copy(f"{Q}/cases/{stem}{suf}", out)
md = f"{out}/{stem}.md"; orig = open(md, encoding="utf-8").read()
t = time.time()
r = subprocess.run([f"{app}/venv/bin/python", f"{app}/live.py", "--restyle", md], capture_output=True, text=True, env={**os.environ, "TIMMY_APP_DIR": app})
new = open(md, encoding="utf-8").read()
print(f"== {name}  ({time.time()-t:.0f}s) rc={r.returncode} stdout={r.stdout.strip()} stderr={r.stderr.strip()[-300:]}")
ol, nl = orig.splitlines(), new.splitlines()
date = next(l for l in ol if l.startswith("**תאריך:**")); trl = next(l for l in ol if l.startswith("תמלול מלא:"))
print("title ok:", nl[0].startswith("# "), "|", nl[0])
print("date line exact:", date in nl)
print("transcript line exact:", trl in nl, "| last nonempty is transcript line:", [l for l in nl if l.strip()][-1] == trl)
print("em dash present:", "—" in new)
print("changed:", new != orig)
print("----- NEW -----"); print(new)

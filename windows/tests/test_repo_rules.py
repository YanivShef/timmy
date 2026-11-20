"""Public-repo and UI-text rules for everything under windows/."""
import re

LONG_DASH = chr(0x2014)  # the long dash, written as a code point so this file passes its own check
TEXT_EXT = {".py", ".md", ".toml", ".ps1", ".html", ".json", ".txt", ".yml", ".yaml", ".spec", ".iss", ".cfg", ".ini"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL = ("noreply@anthropic.com", "@example.com", "@example.org", "@s.whatsapp.net")


def text_files(root):
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in TEXT_EXT and "out" not in p.relative_to(root).parts[:1]:
            yield p


def test_no_long_dash_anywhere(repo_root):
    bad = [str(p) for p in text_files(repo_root / "windows") if LONG_DASH in p.read_text(encoding="utf-8", errors="ignore")]
    wf = repo_root / ".github" / "workflows" / "windows.yml"   # windows/ci/windows.yml until the workflow is installed
    if wf.exists() and LONG_DASH in wf.read_text(encoding="utf-8"):
        bad.append(str(wf))
    assert not bad, f"long dash found in: {bad}"


def test_no_real_emails(repo_root):
    bad = []
    for p in text_files(repo_root / "windows"):
        for m in EMAIL.findall(p.read_text(encoding="utf-8", errors="ignore")):
            if not any(a in m for a in ALLOWED_EMAIL):
                bad.append(f"{p.name}: {m}")
    assert not bad, bad


def test_workflow_exists_once(repo_root):
    """The CI workflow lives in exactly one place: .github/workflows/ (installed) or windows/ci/ (waiting for scope)."""
    places = [p for p in (repo_root / ".github" / "workflows" / "windows.yml", repo_root / "windows" / "ci" / "windows.yml") if p.exists()]
    assert len(places) == 1, places

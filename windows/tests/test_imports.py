import importlib
import pkgutil

import timmy


def all_modules():
    yield "timmy"
    for m in pkgutil.walk_packages(timmy.__path__, "timmy."):
        if "._mac" in m.name:      # vendored Mac code is imported by its own tests (build step 1+)
            continue
        yield m.name


def test_every_module_imports_on_any_os():
    # Windows-only APIs must be imported lazily so the package (and its tests) load on any CI host
    try:
        import PySide6  # noqa: F401  (the UI modules need the "ui" extra; jobs without it still check the rest)
        have_qt = True
    except ImportError:
        have_qt = False
    names = list(all_modules())
    assert "timmy.engine.protocol" in names and "timmy.ui.app" in names
    for name in names:
        if not have_qt and (name.startswith("timmy.ui") or name == "timmy.winsys.instance"):
            continue
        importlib.import_module(name)


def test_entry_point_reports_environment(capsys, app_dir):
    from timmy.__main__ import main
    assert main(["--report"]) == 0
    out = capsys.readouterr().out
    assert '"version"' in out and str(app_dir).replace("\\", "\\\\") in out


def test_version_flag(capsys):
    from timmy.__main__ import main
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == timmy.__version__
